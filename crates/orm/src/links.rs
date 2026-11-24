//! Many-to-many link writes: add, remove and replace rows of a bridge table
//! inside one transaction. Keys are coerced and de-duplicated here, targets are
//! resolved inside the caller's scope before anything is written, and every
//! identifier is quoted by the query compiler; keys travel only as parameters.

use std::collections::{BTreeSet, HashMap, HashSet};

use uuid::Uuid;

use crate::error::{DbError, ErrorKind};
use crate::query::{
    compile_delete_with, compile_select_with, quote_with, render_joins, Builder, Delete, Dialect,
    Filter, Join, Op, QueryError, Select, SubFilter,
};
use crate::{Row, Transaction, Value};

/// Most keys one call may name, and most links one owner may hold.
pub const MAX_LINK_ITEMS: usize = 10_000;

/// Keys per `IN` list or multi-row `INSERT`.
const CHUNK: usize = 1000;

/// How the keys of a table are typed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum KeyKind {
    Uuid,
    Int,
    Text { max_length: usize },
}

/// A key as the caller supplied it, before it is checked against a [`KeyKind`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RawKey {
    Int(i64),
    Text(String),
    Uuid(Uuid),
    /// Any other kind of value.
    Invalid,
}

/// A key in its canonical form, ordered like the primary key column.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Canon {
    Int(i64),
    Text(String),
    Uuid(Uuid),
}

impl Canon {
    pub fn to_value(&self) -> Value {
        match self {
            Self::Int(n) => Value::Int(*n),
            Self::Text(text) => Value::Text(text.clone()),
            Self::Uuid(id) => Value::Uuid(*id),
        }
    }
}

/// Why a list of keys was refused.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum KeyFailure {
    /// The key at this position has the wrong type or shape.
    Invalid(usize),
    /// More distinct keys than the limit allows.
    TooMany,
}

impl KeyKind {
    /// The canonical key for `raw`, or `None` when it does not fit this kind.
    pub fn coerce(self, raw: &RawKey) -> Option<Canon> {
        match (self, raw) {
            (Self::Uuid, RawKey::Uuid(id)) => Some(Canon::Uuid(*id)),
            (Self::Uuid, RawKey::Text(text)) => parse_uuid(text).map(Canon::Uuid),
            (Self::Int, RawKey::Int(n)) => Some(Canon::Int(*n)),
            (Self::Text { max_length }, RawKey::Text(text)) => {
                (text.chars().count() <= max_length).then(|| Canon::Text(text.clone()))
            }
            _ => None,
        }
    }

    /// The canonical key of a value read back from the database.
    pub fn read(self, value: &Value) -> Option<Canon> {
        match (self, value) {
            (Self::Uuid, Value::Uuid(id)) => Some(Canon::Uuid(*id)),
            (Self::Uuid, Value::Text(text)) => parse_uuid(text).map(Canon::Uuid),
            (Self::Uuid, Value::Bytes(bytes)) => Uuid::from_slice(bytes).ok().map(Canon::Uuid),
            (Self::Int, Value::Int(n)) => Some(Canon::Int(*n)),
            (Self::Text { .. }, Value::Text(text)) => Some(Canon::Text(text.clone())),
            _ => None,
        }
    }
}

/// Only the canonical hyphenated spelling counts as a UUID string.
fn parse_uuid(text: &str) -> Option<Uuid> {
    let id = Uuid::try_parse(text).ok()?;
    (text.len() == 36).then_some(id)
}

/// Coerce, de-duplicate and sort `keys`; at most `max_items` distinct keys.
pub fn coerce_keys(
    kind: KeyKind,
    keys: &[RawKey],
    max_items: usize,
) -> Result<Vec<Canon>, KeyFailure> {
    let mut distinct = BTreeSet::new();
    for (index, raw) in keys.iter().enumerate() {
        let key = kind.coerce(raw).ok_or(KeyFailure::Invalid(index))?;
        distinct.insert(key);
        if distinct.len() > max_items {
            return Err(KeyFailure::TooMany);
        }
    }
    Ok(distinct.into_iter().collect())
}

/// The tables and columns of one relation. Every name is checked when the
/// value is built.
#[derive(Debug, Clone)]
pub struct LinkTable {
    pub bridge: String,
    pub bridge_pk: String,
    pub owner_table: String,
    pub owner_pk: String,
    pub owner_column: String,
    pub target_table: String,
    pub target_pk: String,
    pub target_column: String,
    pub owner_kind: KeyKind,
    pub target_kind: KeyKind,
    pub max_items: usize,
    /// On the reverse end of a relation: the `max_items` its declaring end
    /// enforces on the rows this end links to.
    pub declared_max: Option<usize>,
    /// The target's soft-delete column: a link to a target marked deleted does
    /// not count towards `max_items`.
    pub target_live: Option<String>,
    /// The owner's soft-delete column: on the reverse end, a link held by an
    /// owner marked deleted does not count towards the declaring end's limit.
    pub owner_live: Option<String>,
}

impl LinkTable {
    pub fn validate(&self) -> Result<(), QueryError> {
        let names = [
            &self.bridge,
            &self.bridge_pk,
            &self.owner_table,
            &self.owner_pk,
            &self.owner_column,
            &self.target_table,
            &self.target_pk,
            &self.target_column,
        ];
        let live = self.target_live.iter().chain(&self.owner_live);
        for name in names.into_iter().chain(live) {
            quote_with(name, Dialect::Postgres)?;
        }
        if self.max_items == 0 || self.max_items > MAX_LINK_ITEMS {
            return Err(QueryError::BadFilter("max_items out of range".into()));
        }
        Ok(())
    }
}

/// The targets a caller may see: joins and conditions on the target table.
#[derive(Debug, Clone, Default)]
pub struct Scope {
    pub joins: Vec<Join>,
    pub filters: Vec<Filter<Value>>,
    pub subfilters: Vec<SubFilter<Value>>,
}

impl Scope {
    /// A scope that holds the targets whose `column` equals `value`.
    pub fn column_equals(column: &str, value: Value) -> Self {
        Self {
            joins: Vec::new(),
            filters: vec![Filter {
                column: column.to_owned(),
                op: Op::Eq,
                values: vec![value],
            }],
            subfilters: Vec::new(),
        }
    }

    pub fn is_open(&self) -> bool {
        self.joins.is_empty() && self.filters.is_empty() && self.subfilters.is_empty()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LinkOp {
    Add,
    Remove,
    Set,
}

/// One write: `keys` are already coerced and unique.
#[derive(Debug, Clone)]
pub struct LinkWrite {
    pub op: LinkOp,
    pub owner: Canon,
    pub keys: Vec<Canon>,
    pub scope: Scope,
    /// Conditions on the owner row, checked after its lock is taken: a row
    /// that left them while the caller waited is reported as missing.
    pub owner_scope: Scope,
}

#[derive(Debug, Clone, PartialEq)]
pub enum LinkOutcome {
    Done {
        added: Vec<Value>,
        removed: Vec<Value>,
    },
    /// The owner row is gone (deleted while the write waited for its lock).
    OwnerGone,
    /// The owner row exists but no longer meets the caller's conditions.
    OwnerMissing,
    /// This many of the keys do not exist in scope.
    Missing(usize),
    /// The owner would hold more than `max_items` links.
    LimitExceeded,
}

fn compile_error(err: QueryError) -> DbError {
    DbError::new(ErrorKind::Other, err.to_string())
}

struct Ctx<'a> {
    tx: &'a Transaction,
    dialect: Dialect,
    table: &'a LinkTable,
    owner: &'a Canon,
    scope: &'a Scope,
    owner_scope: &'a Scope,
}

/// Apply `write` on `tx`. The caller owns the transaction and rolls it back on
/// anything but `Done`. `new_id` makes the primary key of each new bridge row.
pub async fn apply(
    tx: &Transaction,
    dialect: Dialect,
    table: &LinkTable,
    write: &LinkWrite,
    new_id: &mut (dyn FnMut() -> Uuid + Send),
) -> Result<LinkOutcome, DbError> {
    table.validate().map_err(compile_error)?;
    let ctx = Ctx {
        tx,
        dialect,
        table,
        owner: &write.owner,
        scope: &write.scope,
        owner_scope: &write.owner_scope,
    };
    // One global order of acquisition, whichever end writes: the declaring
    // table first, then the other. From the reverse end that means the
    // declaring rows before the owner row, so two writers never wait for each
    // other's rows.
    if let (Some(_), LinkOp::Add | LinkOp::Set) = (table.declared_max, write.op) {
        ctx.lock_declarers(&write.keys).await?;
    }
    if let Some(refusal) = ctx.lock_owner().await? {
        return Ok(refusal);
    }
    ctx.guarded(write, new_id).await
}

const SAVEPOINT: &str = "SAVEPOINT hinoki_links";
const ROLLBACK_TO: &str = "ROLLBACK TO SAVEPOINT hinoki_links";
const RELEASE: &str = "RELEASE SAVEPOINT hinoki_links";

impl Ctx<'_> {
    /// Run the write and its checks inside a savepoint: whatever is not `Done`
    /// leaves nothing behind, even when the caller's transaction goes on to
    /// commit.
    async fn guarded(
        &self,
        write: &LinkWrite,
        new_id: &mut (dyn FnMut() -> Uuid + Send),
    ) -> Result<LinkOutcome, DbError> {
        self.tx.execute(SAVEPOINT, &[]).await?;
        let outcome = self.write_then_check(write, new_id).await;
        if matches!(outcome, Ok(LinkOutcome::Done { .. })) {
            self.tx.execute(RELEASE, &[]).await?;
            return outcome;
        }
        let mut undone = self.tx.execute(ROLLBACK_TO, &[]).await;
        if undone.is_ok() {
            // ROLLBACK TO keeps the savepoint: release it so none accumulate.
            undone = self.tx.execute(RELEASE, &[]).await;
        }
        match (outcome, undone) {
            (Err(error), _) => Err(error),
            (Ok(outcome), Ok(_)) => Ok(outcome),
            (Ok(_), Err(error)) => Err(error),
        }
    }
}

impl Ctx<'_> {
    /// The write and its limit checks, in the savepoint of [`Ctx::guarded`].
    async fn write_then_check(
        &self,
        write: &LinkWrite,
        new_id: &mut (dyn FnMut() -> Uuid + Send),
    ) -> Result<LinkOutcome, DbError> {
        let table = self.table;
        let step = match write.op {
            LinkOp::Add => self.add(&write.keys, new_id).await?,
            LinkOp::Remove => self.remove(&write.keys).await?,
            LinkOp::Set => self.set(&write.keys, new_id).await?,
        };
        match step {
            Step::Missing(count) => Ok(LinkOutcome::Missing(count)),
            Step::Changed { added, removed } => {
                if !added.is_empty() && self.count().await? > table.max_items {
                    return Ok(LinkOutcome::LimitExceeded);
                }
                if let (false, Some(limit)) = (added.is_empty(), table.declared_max) {
                    if self.declarer_over(&added, limit).await? {
                        return Ok(LinkOutcome::LimitExceeded);
                    }
                }
                Ok(LinkOutcome::Done {
                    added: values(&added),
                    removed: values(&removed),
                })
            }
        }
    }
}

fn values(keys: &[Canon]) -> Vec<Value> {
    keys.iter().map(Canon::to_value).collect()
}

enum Step {
    Changed {
        added: Vec<Canon>,
        removed: Vec<Canon>,
    },
    /// This many of the named keys are not targets in scope.
    Missing(usize),
}

impl Ctx<'_> {
    fn owner_value(&self) -> Value {
        self.owner.to_value()
    }

    async fn fetch(&self, sql: &str, params: &[Value]) -> Result<Vec<Row>, DbError> {
        self.tx.fetch(sql, params).await
    }

    fn q(&self, name: &str) -> Result<String, DbError> {
        quote_with(name, self.dialect).map_err(compile_error)
    }

    fn keys_of(&self, rows: &[Row], column: usize, kind: KeyKind) -> Result<Vec<Canon>, DbError> {
        let mut keys = BTreeSet::new();
        for row in rows {
            let key = row.get(column).and_then(|(_, value)| kind.read(value));
            keys.insert(key.ok_or_else(|| DbError::new(ErrorKind::Type, "unreadable link key"))?);
        }
        Ok(keys.into_iter().collect())
    }

    /// Lock the owner row so writers of one owner run one after the other.
    /// `Some` is the refusal: the row is gone or left the caller's scope.
    async fn lock_owner(&self) -> Result<Option<LinkOutcome>, DbError> {
        let t = self.table;
        let lock = if self.dialect == Dialect::Sqlite {
            ""
        } else {
            " FOR UPDATE"
        };
        let sql = format!(
            "SELECT {pk} FROM {table} WHERE {pk} = {ph}{lock}",
            pk = self.q(&t.owner_pk)?,
            table = self.q(&t.owner_table)?,
            ph = placeholder(self.dialect, 1),
        );
        if self.fetch(&sql, &[self.owner_value()]).await?.is_empty() {
            return Ok(Some(LinkOutcome::OwnerGone));
        }
        Ok((!self.owner_in_scope().await?).then_some(LinkOutcome::OwnerMissing))
    }

    /// Whether the locked owner row still meets the caller's conditions. The
    /// read is current on every backend (locking on MySQL and MariaDB, whose
    /// plain reads use the snapshot of the transaction's first read).
    async fn owner_in_scope(&self) -> Result<bool, DbError> {
        if self.owner_scope.is_open() {
            return Ok(true);
        }
        let t = self.table;
        let mut filters = vec![Filter {
            column: t.owner_pk.clone(),
            op: Op::Eq,
            values: vec![self.owner_value()],
        }];
        filters.extend(self.owner_scope.filters.iter().cloned());
        let select = Select {
            table: t.owner_table.clone(),
            columns: vec![t.owner_pk.clone()],
            joins: self.owner_scope.joins.clone(),
            filters,
            subfilters: self.owner_scope.subfilters.clone(),
            keyset: None,
            order: Vec::new(),
            limit: None,
            offset: None,
            count: false,
        };
        let (mut sql, params) =
            compile_select_with(&select, self.dialect).map_err(compile_error)?;
        sql.push_str(self.locking(" FOR UPDATE"));
        Ok(!self.fetch(&sql, &params).await?.is_empty())
    }

    /// The locking suffix of a read that decides a write. MySQL and MariaDB
    /// run a transaction on one snapshot (REPEATABLE READ), so a plain read
    /// could miss what a writer that held the owner lock just committed.
    fn locking(&self, clause: &'static str) -> &'static str {
        if self.dialect == Dialect::Mysql {
            clause
        } else {
            ""
        }
    }

    /// Lock the rows of the declaring end that `keys` name and the caller may
    /// see, in key order, so their own `max_items` cannot be raced past from
    /// this end. MySQL locks every row a locking read examines, scope or not,
    /// so it reads the scope unlocked first and locks only rows that passed.
    async fn lock_declarers(&self, keys: &[Canon]) -> Result<(), DbError> {
        if self.dialect == Dialect::Sqlite {
            return Ok(());
        }
        for chunk in keys.chunks(CHUNK) {
            let narrowed;
            let chunk = if self.dialect == Dialect::Mysql {
                let (sql, params) = self.declarers_sql(chunk, false)?;
                let rows = self.fetch(&sql, &params).await?;
                narrowed = self.keys_of(&rows, 0, self.table.target_kind)?;
                if narrowed.is_empty() {
                    continue;
                }
                &narrowed[..]
            } else {
                chunk
            };
            let (sql, params) = self.declarers_sql(chunk, true)?;
            self.fetch(&sql, &params).await?;
        }
        Ok(())
    }

    fn declarers_sql(&self, chunk: &[Canon], lock: bool) -> Result<(String, Vec<Value>), DbError> {
        let t = self.table;
        let mut b = Builder::<Value>::new(String::new(), self.dialect);
        b.base = Some(t.target_table.clone());
        let table = self.q(&t.target_table)?;
        let pk = format!("{table}.{}", self.q(&t.target_pk)?);
        let mut sql = format!("SELECT {pk} FROM {table}");
        sql.push_str(
            &render_joins(&self.scope.joins, &t.target_table, self.dialect)
                .map_err(compile_error)?,
        );
        let mut filters = vec![Filter {
            column: t.target_pk.clone(),
            op: Op::In,
            values: chunk.iter().map(Canon::to_value).collect(),
        }];
        filters.extend(self.scope.filters.iter().cloned());
        let clause = b
            .where_clause(&filters, None, &self.scope.subfilters)
            .map_err(compile_error)?;
        sql.push_str(&clause);
        sql.push_str(&format!(" ORDER BY {pk}"));
        if lock {
            sql.push_str(&match self.dialect {
                Dialect::Postgres => format!(" FOR UPDATE OF {table}"),
                _ => " FOR UPDATE".to_owned(),
            });
        }
        let Builder { params, .. } = b;
        Ok((sql, params))
    }

    /// Whether any of the declaring rows in `keys` holds more than `limit` links.
    async fn declarer_over(&self, keys: &[Canon], limit: usize) -> Result<bool, DbError> {
        let t = self.table;
        for chunk in keys.chunks(CHUNK) {
            let holders: Vec<String> = (1..=chunk.len())
                .map(|n| placeholder(self.dialect, n))
                .collect();
            let column = self.q(&t.target_column)?;
            let sql = format!(
                "SELECT {column} FROM {} WHERE {column} IN ({}){} GROUP BY {column} \
                 HAVING count(*) > {limit}{}",
                self.q(&t.bridge)?,
                holders.join(", "),
                self.live_owners()?,
                self.locking(" FOR UPDATE"),
            );
            if !self.fetch(&sql, &values(chunk)).await?.is_empty() {
                return Ok(true);
            }
        }
        Ok(false)
    }

    /// ` AND owner IN (live owners)` when the owner soft-deletes, else nothing.
    fn live_owners(&self) -> Result<String, DbError> {
        let t = self.table;
        let Some(live) = &t.owner_live else {
            return Ok(String::new());
        };
        Ok(format!(
            " AND {} IN (SELECT {} FROM {} WHERE {} IS NULL)",
            self.q(&t.owner_column)?,
            self.q(&t.owner_pk)?,
            self.q(&t.owner_table)?,
            self.q(live)?,
        ))
    }

    /// The links this owner holds to targets that are not soft-deleted, whatever
    /// the caller's scope: `max_items` bounds every load of the relation.
    async fn count(&self) -> Result<usize, DbError> {
        let (mut sql, params) = count_select(self.table, self.dialect, self.owner_value())?;
        sql.push_str(self.locking(" FOR UPDATE"));
        let rows = self.fetch(&sql, &params).await?;
        match rows.first().and_then(|row| row.first()) {
            Some((_, Value::Int(n))) => Ok(usize::try_from(*n).unwrap_or(usize::MAX)),
            _ => Err(DbError::new(ErrorKind::Type, "unreadable link count")),
        }
    }

    /// Each named key that is a target in scope, with whether the owner already
    /// links it.
    async fn resolve(&self, keys: &[Canon]) -> Result<HashMap<Canon, bool>, DbError> {
        let mut found = HashMap::new();
        for chunk in keys.chunks(CHUNK) {
            let narrowed;
            let chunk = if self.dialect == Dialect::Mysql {
                // MySQL locks every row a locking read examines, scope or not:
                // read the scope unlocked, then lock only rows the caller sees.
                narrowed = self
                    .resolve_chunk(chunk, false)
                    .await?
                    .into_iter()
                    .map(|(key, _)| key)
                    .collect::<Vec<_>>();
                if narrowed.is_empty() {
                    continue;
                }
                &narrowed[..]
            } else {
                chunk
            };
            found.extend(self.resolve_chunk(chunk, true).await?);
        }
        Ok(found)
    }

    async fn resolve_chunk(
        &self,
        chunk: &[Canon],
        lock: bool,
    ) -> Result<Vec<(Canon, bool)>, DbError> {
        let (sql, params) = self.resolve_sql(chunk, lock)?;
        let mut found = Vec::new();
        for row in self.fetch(&sql, &params).await? {
            let key = row
                .first()
                .and_then(|(_, v)| self.table.target_kind.read(v));
            let linked = match row.get(1) {
                Some((_, Value::Bool(flag))) => *flag,
                Some((_, Value::Int(n))) => *n != 0,
                _ => return Err(DbError::new(ErrorKind::Type, "unreadable link flag")),
            };
            let key = key.ok_or_else(|| DbError::new(ErrorKind::Type, "unreadable key"))?;
            found.push((key, linked));
        }
        Ok(found)
    }

    fn bridge_alias(&self) -> String {
        let mut alias = "hinoki_link".to_owned();
        let mut number = 0usize;
        while alias == self.table.target_table
            || self.scope.joins.iter().any(|join| join.alias == alias)
        {
            number += 1;
            alias = format!("hinoki_link_{number}");
        }
        alias
    }

    fn resolve_sql(&self, chunk: &[Canon], lock: bool) -> Result<(String, Vec<Value>), DbError> {
        let t = self.table;
        let mut b = Builder::<Value>::new(String::new(), self.dialect);
        let target_pk = format!("{}.{}", self.q(&t.target_table)?, self.q(&t.target_pk)?);
        let bridge_alias = self.bridge_alias();
        let link_t = format!("{bridge_alias}.{}", t.target_column);
        let link_o = format!("{bridge_alias}.{}", t.owner_column);
        let owner = b.placeholder(self.owner_value());
        let mut sql = format!(
            "SELECT {target_pk} AS {k}, ({link} IS NOT NULL) AS {f} FROM {target} \
             LEFT JOIN {bridge} AS {alias} ON {link} = {target_pk} AND {link_o_q} = {owner}",
            k = self.q("hinoki_k")?,
            f = self.q("hinoki_linked")?,
            link = crate::query::column_ref(&link_t, None, self.dialect).map_err(compile_error)?,
            link_o_q =
                crate::query::column_ref(&link_o, None, self.dialect).map_err(compile_error)?,
            target = self.q(&t.target_table)?,
            bridge = self.q(&t.bridge)?,
            alias = self.q(&bridge_alias)?,
        );
        sql.push_str(
            &render_joins(&self.scope.joins, &t.target_table, self.dialect)
                .map_err(compile_error)?,
        );
        b.base = Some(t.target_table.clone());
        let mut filters = vec![Filter {
            column: t.target_pk.clone(),
            op: Op::In,
            values: chunk.iter().map(Canon::to_value).collect(),
        }];
        filters.extend(self.scope.filters.iter().cloned());
        let clause = b
            .where_clause(&filters, None, &self.scope.subfilters)
            .map_err(compile_error)?;
        sql.push_str(&clause);
        if lock {
            sql.push_str(&self.share_targets()?);
        }
        let Builder { params, .. } = b;
        Ok((sql, params))
    }

    /// The lock that keeps the resolved targets from being deleted before the
    /// bridge rows that point at them are written.
    fn share_targets(&self) -> Result<String, DbError> {
        Ok(match self.dialect {
            Dialect::Mysql => " LOCK IN SHARE MODE".to_owned(),
            Dialect::Postgres => format!(" FOR KEY SHARE OF {}", self.q(&self.table.target_table)?),
            Dialect::Sqlite => String::new(),
        })
    }

    /// The targets of this owner that are in scope.
    async fn current(&self) -> Result<Vec<Canon>, DbError> {
        let (mut sql, params) = self.linked_select(None)?;
        sql.push_str(self.locking(" FOR UPDATE"));
        let rows = self.fetch(&sql, &params).await?;
        self.keys_of(&rows, 0, self.table.target_kind)
    }

    /// `SELECT target FROM bridge WHERE owner = ? [AND target IN keys] [AND target
    /// IN (SELECT pk FROM target ... scope)]`.
    fn linked_select(&self, keys: Option<&[Canon]>) -> Result<(String, Vec<Value>), DbError> {
        let t = self.table;
        let mut filters = vec![Filter {
            column: t.owner_column.clone(),
            op: Op::Eq,
            values: vec![self.owner_value()],
        }];
        if let Some(keys) = keys {
            filters.push(Filter {
                column: t.target_column.clone(),
                op: Op::In,
                values: keys.iter().map(Canon::to_value).collect(),
            });
        }
        let select = Select {
            table: t.bridge.clone(),
            columns: vec![t.target_column.clone()],
            joins: Vec::new(),
            filters,
            subfilters: self.scope_subfilter(),
            keyset: None,
            order: Vec::new(),
            limit: None,
            offset: None,
            count: false,
        };
        compile_select_with(&select, self.dialect).map_err(compile_error)
    }

    fn scope_subfilter(&self) -> Vec<SubFilter<Value>> {
        scope_subfilter(self.table, self.scope)
    }

    async fn add(
        &self,
        keys: &[Canon],
        new_id: &mut (dyn FnMut() -> Uuid + Send),
    ) -> Result<Step, DbError> {
        let found = self.resolve(keys).await?;
        if found.len() < keys.len() {
            return Ok(Step::Missing(keys.len() - found.len()));
        }
        let fresh: Vec<Canon> = keys
            .iter()
            .filter(|k| found.get(*k) == Some(&false))
            .cloned()
            .collect();
        let added = self.insert(&fresh, new_id).await?;
        Ok(Step::Changed {
            added,
            removed: Vec::new(),
        })
    }

    async fn remove(&self, keys: &[Canon]) -> Result<Step, DbError> {
        let removed = self.delete(keys, true).await?;
        Ok(Step::Changed {
            added: Vec::new(),
            removed,
        })
    }

    async fn set(
        &self,
        keys: &[Canon],
        new_id: &mut (dyn FnMut() -> Uuid + Send),
    ) -> Result<Step, DbError> {
        let found = self.resolve(keys).await?;
        if found.len() < keys.len() {
            return Ok(Step::Missing(keys.len() - found.len()));
        }
        let wanted: HashSet<&Canon> = keys.iter().collect();
        let current = self.current().await?;
        let stale: Vec<Canon> = current
            .iter()
            .filter(|k| !wanted.contains(k))
            .cloned()
            .collect();
        let held: HashSet<&Canon> = current.iter().collect();
        let fresh: Vec<Canon> = keys.iter().filter(|k| !held.contains(k)).cloned().collect();
        let removed = self.delete(&stale, false).await?;
        let added = self.insert(&fresh, new_id).await?;
        Ok(Step::Changed { added, removed })
    }

    /// Unlink `keys`; with `scoped`, only those that are targets in scope.
    async fn delete(&self, keys: &[Canon], scoped: bool) -> Result<Vec<Canon>, DbError> {
        let mut removed = BTreeSet::new();
        for chunk in keys.chunks(CHUNK) {
            removed.extend(self.delete_chunk(chunk, scoped).await?);
        }
        Ok(removed.into_iter().collect())
    }

    async fn delete_chunk(&self, chunk: &[Canon], scoped: bool) -> Result<Vec<Canon>, DbError> {
        let t = self.table;
        let mut chunk = chunk.to_vec();
        let returning = self.dialect != Dialect::Mysql;
        if !returning && scoped {
            // MySQL cannot return deleted rows: read the ones in scope first.
            let (mut sql, params) = self.linked_select(Some(&chunk))?;
            sql.push_str(" FOR UPDATE");
            chunk = self.keys_of(&self.fetch(&sql, &params).await?, 0, t.target_kind)?;
        }
        if chunk.is_empty() {
            return Ok(chunk);
        }
        let delete = Delete {
            table: t.bridge.clone(),
            filters: vec![
                Filter {
                    column: t.owner_column.clone(),
                    op: Op::Eq,
                    values: vec![self.owner_value()],
                },
                Filter {
                    column: t.target_column.clone(),
                    op: Op::In,
                    values: chunk.iter().map(Canon::to_value).collect(),
                },
            ],
            subfilters: if scoped && returning {
                self.scope_subfilter()
            } else {
                Vec::new()
            },
            returning: if returning {
                vec![t.target_column.clone()]
            } else {
                Vec::new()
            },
        };
        let (sql, params) = compile_delete_with(&delete, self.dialect).map_err(compile_error)?;
        if !returning {
            self.tx.execute(&sql, &params).await?;
            return Ok(chunk);
        }
        let rows = self.fetch(&sql, &params).await?;
        self.keys_of(&rows, 0, t.target_kind)
    }

    /// Link `keys` (all targets in scope, none linked yet); returns those added.
    async fn insert(
        &self,
        keys: &[Canon],
        new_id: &mut (dyn FnMut() -> Uuid + Send),
    ) -> Result<Vec<Canon>, DbError> {
        let mut added = Vec::new();
        for chunk in keys.chunks(CHUNK) {
            let (sql, params) = self.insert_sql(chunk, new_id)?;
            if self.dialect == Dialect::Mysql {
                self.tx.execute(&sql, &params).await?;
                added.extend(chunk.iter().cloned());
            } else {
                let rows = self.fetch(&sql, &params).await?;
                added.extend(self.keys_of(&rows, 0, self.table.target_kind)?);
            }
        }
        added.sort();
        Ok(added)
    }

    fn insert_sql(
        &self,
        chunk: &[Canon],
        new_id: &mut (dyn FnMut() -> Uuid + Send),
    ) -> Result<(String, Vec<Value>), DbError> {
        let t = self.table;
        let mut b = Builder::<Value>::new(String::new(), self.dialect);
        let rows: Vec<String> = chunk
            .iter()
            .map(|key| {
                let id = b.placeholder(Value::Uuid(new_id()));
                let owner = b.placeholder(self.owner_value());
                let target = b.placeholder(key.to_value());
                format!("({id}, {owner}, {target})")
            })
            .collect();
        let (owner, target) = (self.q(&t.owner_column)?, self.q(&t.target_column)?);
        let tail = match self.dialect {
            Dialect::Mysql => format!(" ON DUPLICATE KEY UPDATE {target} = {target}"),
            _ => format!(" ON CONFLICT ({owner}, {target}) DO NOTHING RETURNING {target}"),
        };
        let sql = format!(
            "INSERT INTO {} ({}, {owner}, {target}) VALUES {}{tail}",
            self.q(&t.bridge)?,
            self.q(&t.bridge_pk)?,
            rows.join(", "),
        );
        Ok((sql, b.params))
    }
}

/// The condition that keeps a bridge statement on targets in scope.
fn scope_subfilter(table: &LinkTable, scope: &Scope) -> Vec<SubFilter<Value>> {
    if scope.is_open() {
        return Vec::new();
    }
    vec![SubFilter {
        column: table.target_column.clone(),
        negated: false,
        select: Box::new(Select {
            table: table.target_table.clone(),
            columns: vec![table.target_pk.clone()],
            joins: scope.joins.clone(),
            filters: scope.filters.clone(),
            subfilters: scope.subfilters.clone(),
            keyset: None,
            order: Vec::new(),
            limit: None,
            offset: None,
            count: false,
        }),
    }]
}

/// `SELECT count(*)` of one owner's links to live targets: with a soft-delete
/// column, a link to a deleted target is not counted.
fn count_select(
    table: &LinkTable,
    dialect: Dialect,
    owner: Value,
) -> Result<(String, Vec<Value>), DbError> {
    let live = table.target_live.iter().map(|column| Filter {
        column: column.clone(),
        op: Op::IsNull,
        values: Vec::new(),
    });
    let scope = Scope {
        filters: live.collect(),
        ..Scope::default()
    };
    let select = Select {
        table: table.bridge.clone(),
        columns: Vec::new(),
        joins: Vec::new(),
        filters: vec![Filter {
            column: table.owner_column.clone(),
            op: Op::Eq,
            values: vec![owner],
        }],
        subfilters: scope_subfilter(table, &scope),
        keyset: None,
        order: Vec::new(),
        limit: None,
        offset: None,
        count: true,
    };
    compile_select_with(&select, dialect).map_err(compile_error)
}

/// The links of every owner in `owners` that point at a target in `scope`, as
/// one statement: `SELECT owner, target FROM bridge WHERE owner IN (...) [AND
/// target IN (scope)]`. Read the rows with [`read_ids`].
pub fn ids_select(
    table: &LinkTable,
    dialect: Dialect,
    scope: &Scope,
    owners: &[Canon],
) -> Result<(String, Vec<Value>), DbError> {
    table.validate().map_err(compile_error)?;
    let select = Select {
        table: table.bridge.clone(),
        columns: vec![table.owner_column.clone(), table.target_column.clone()],
        joins: Vec::new(),
        filters: vec![Filter {
            column: table.owner_column.clone(),
            op: Op::In,
            values: owners.iter().map(Canon::to_value).collect(),
        }],
        subfilters: scope_subfilter(table, scope),
        keyset: None,
        order: Vec::new(),
        limit: Some(owners.len() as u64 * table.max_items as u64 + 1),
        offset: None,
        count: false,
    };
    compile_select_with(&select, dialect).map_err(compile_error)
}

/// The target keys of each owner out of the rows of [`ids_select`], sorted. An
/// owner holding more than `max_items` links means the stored data broke the
/// bound (or `ids_select` was cut by its limit): an error, not a short list.
pub fn read_ids(table: &LinkTable, rows: &[Row]) -> Result<HashMap<Canon, Vec<Canon>>, DbError> {
    let unreadable = || DbError::new(ErrorKind::Type, "unreadable link key");
    let mut found: HashMap<Canon, BTreeSet<Canon>> = HashMap::new();
    for row in rows {
        let owner = row
            .first()
            .and_then(|(_, value)| table.owner_kind.read(value))
            .ok_or_else(unreadable)?;
        let target = row
            .get(1)
            .and_then(|(_, value)| table.target_kind.read(value))
            .ok_or_else(unreadable)?;
        found.entry(owner).or_default().insert(target);
    }
    if found
        .values()
        .any(|targets| targets.len() > table.max_items)
    {
        return Err(DbError::new(
            ErrorKind::Other,
            "stored links exceed max_items",
        ));
    }
    Ok(found
        .into_iter()
        .map(|(owner, targets)| (owner, targets.into_iter().collect()))
        .collect())
}

fn placeholder(dialect: Dialect, n: usize) -> String {
    match dialect {
        Dialect::Postgres => format!("${n}"),
        Dialect::Sqlite => format!("?{n}"),
        Dialect::Mysql => "?".to_owned(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const ID: &str = "123e4567-e89b-12d3-a456-426614174000";

    #[test]
    fn keys_are_deduplicated_and_sorted() {
        let keys = [RawKey::Int(3), RawKey::Int(1), RawKey::Int(3)];
        let got = coerce_keys(KeyKind::Int, &keys, 10).unwrap();
        assert_eq!(got, vec![Canon::Int(1), Canon::Int(3)]);
    }

    #[test]
    fn a_wrong_kind_names_the_position() {
        let keys = [RawKey::Int(1), RawKey::Text("1; DROP TABLE t".into())];
        let got = coerce_keys(KeyKind::Int, &keys, 10);
        assert_eq!(got, Err(KeyFailure::Invalid(1)));
        let got = coerce_keys(KeyKind::Uuid, &[RawKey::Invalid], 10);
        assert_eq!(got, Err(KeyFailure::Invalid(0)));
    }

    #[test]
    fn only_a_canonical_uuid_text_is_accepted() {
        let kind = KeyKind::Uuid;
        let ok = coerce_keys(kind, &[RawKey::Text(ID.into())], 1).unwrap();
        assert_eq!(ok, vec![Canon::Uuid(Uuid::parse_str(ID).unwrap())]);
        let braces = RawKey::Text(format!("{{{ID}}}"));
        assert_eq!(coerce_keys(kind, &[braces], 1), Err(KeyFailure::Invalid(0)));
    }

    #[test]
    fn the_limit_counts_distinct_keys() {
        let same = [RawKey::Int(1), RawKey::Int(1), RawKey::Int(1)];
        assert!(coerce_keys(KeyKind::Int, &same, 1).is_ok());
        let two = [RawKey::Int(1), RawKey::Int(2)];
        assert_eq!(coerce_keys(KeyKind::Int, &two, 1), Err(KeyFailure::TooMany));
    }

    #[test]
    fn text_keys_respect_the_column_length() {
        let kind = KeyKind::Text { max_length: 3 };
        assert!(coerce_keys(kind, &[RawKey::Text("abc".into())], 1).is_ok());
        let long = RawKey::Text("abcd".into());
        assert_eq!(coerce_keys(kind, &[long], 1), Err(KeyFailure::Invalid(0)));
    }

    fn table() -> LinkTable {
        LinkTable {
            bridge: "posts_tags".into(),
            bridge_pk: "id".into(),
            owner_table: "posts".into(),
            owner_pk: "id".into(),
            owner_column: "post_id".into(),
            target_table: "tags".into(),
            target_pk: "id".into(),
            target_column: "tag_id".into(),
            owner_kind: KeyKind::Int,
            target_kind: KeyKind::Int,
            max_items: 3,
            declared_max: None,
            target_live: None,
            owner_live: None,
        }
    }

    #[test]
    fn the_ids_of_a_page_come_from_one_statement_limited_to_the_scope() {
        let owners = [Canon::Int(1), Canon::Int(2)];
        let (open, params) =
            ids_select(&table(), Dialect::Postgres, &Scope::default(), &owners).unwrap();
        assert_eq!(
            open,
            "SELECT \"post_id\", \"tag_id\" FROM \"posts_tags\" WHERE \"post_id\" IN ($1, $2) LIMIT $3"
        );
        assert_eq!(params, vec![Value::Int(1), Value::Int(2), Value::Int(7)]);
        let scope = Scope::column_equals("owner", Value::Text("ann".into()));
        let (scoped, params) = ids_select(&table(), Dialect::Sqlite, &scope, &owners[..1]).unwrap();
        assert!(scoped.contains("\"tag_id\" IN (SELECT \"id\" FROM \"tags\" WHERE"));
        assert_eq!(
            params[params.len() - 2..],
            [Value::Text("ann".into()), Value::Int(4)]
        );
    }

    #[test]
    fn the_link_count_skips_soft_deleted_targets() {
        let (open, params) = count_select(&table(), Dialect::Sqlite, Value::Int(1)).unwrap();
        assert!(!open.contains("IN (SELECT"), "{open}");
        assert_eq!(params, vec![Value::Int(1)]);
        let live = LinkTable {
            target_live: Some("deleted_at".into()),
            ..table()
        };
        let (sql, _) = count_select(&live, Dialect::Postgres, Value::Int(1)).unwrap();
        assert!(
            sql.contains(
                "\"tag_id\" IN (SELECT \"id\" FROM \"tags\" WHERE \"deleted_at\" IS NULL)"
            ),
            "{sql}"
        );
        let bad = LinkTable {
            target_live: Some("bad name".into()),
            ..table()
        };
        assert!(bad.validate().is_err());
        let bad_owner = LinkTable {
            owner_live: Some("bad name".into()),
            ..table()
        };
        assert!(bad_owner.validate().is_err());
    }

    #[test]
    fn the_ids_of_each_owner_are_sorted_and_an_unlinked_owner_has_none() {
        let row = |owner: i64, target: i64| {
            vec![
                ("post_id".to_owned(), Value::Int(owner)),
                ("tag_id".to_owned(), Value::Int(target)),
            ]
        };
        let found = read_ids(&table(), &[row(1, 9), row(2, 4), row(1, 3)]).unwrap();
        assert_eq!(found[&Canon::Int(1)], vec![Canon::Int(3), Canon::Int(9)]);
        assert_eq!(found[&Canon::Int(2)], vec![Canon::Int(4)]);
        assert!(!found.contains_key(&Canon::Int(5)));
        let bad = vec![("post_id".to_owned(), Value::Text("x".into()))];
        assert!(read_ids(&table(), &[bad]).is_err());
    }
}
