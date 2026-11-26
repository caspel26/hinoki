//! The many-to-many fields (`*_ids`) of a native CRUD route: the ids a read
//! shows, and the links a write sets inside the write's own transaction.

use std::collections::HashMap;

use hinoki_orm::links::{self, coerce_keys, Canon, LinkOp, LinkOutcome, LinkWrite, RawKey, Scope};
use hinoki_orm::{DbError, ErrorKind, Transaction, Value};

use crate::crudplan::{CallerContext, CrudPlan, PlanLink};
use crate::jwt::ClaimList;
use crate::nativecrud::{FetchFuture, ReadDatabase};
use crate::rows::RowIds;
use crate::schema::Validated;

/// The most keys one `IN` list carries.
const CHUNK: usize = 1000;

type Row = Vec<(String, Value)>;

/// The links a write sets, as `(index into plan.links, keys)`, and the caller
/// whose scope bounds the targets.
#[derive(Debug, Default)]
pub(crate) struct LinkWork {
    pub(crate) sets: Vec<(usize, Vec<Canon>)>,
    pub(crate) caller: Option<(String, ClaimList)>,
}

impl LinkWork {
    pub(crate) fn caller_context(&self) -> Option<CallerContext<'_>> {
        self.caller
            .as_ref()
            .map(|(subject, claims)| CallerContext { subject, claims })
    }
}

/// The index of the relation `name` is the ids field of.
pub(crate) fn link_index(plan: &CrudPlan, name: &str) -> Option<usize> {
    plan.links.iter().position(|link| link.field == name)
}

/// The canonical keys of a validated ids field, or `None` when the value is
/// not a list of keys the relation accepts (Python then answers).
pub(crate) fn keys_of(link: &PlanLink, value: Validated) -> Option<Vec<Canon>> {
    let Validated::List(items) = value else {
        return None;
    };
    let mut raw = Vec::with_capacity(items.len());
    for item in items {
        raw.push(match item {
            Validated::Int(number) => RawKey::Int(number),
            Validated::Str(text) => RawKey::Text(text),
            Validated::Uuid(id) => RawKey::Uuid(id),
            _ => return None,
        });
    }
    coerce_keys(link.table.target_kind, &raw, link.table.max_items).ok()
}

/// The targets the caller may link or see; `None` when a scoped link has no
/// resolvable scope value for this caller.
pub(crate) fn scope_of(link: &PlanLink, caller: Option<CallerContext<'_>>) -> Option<Scope> {
    match &link.scope {
        None => Some(Scope::default()),
        Some(spec) => spec
            .resolve(caller)
            .map(|value| Scope::column_equals(&spec.column, value)),
    }
}

fn dialect_of(name: &str) -> Result<hinoki_orm::query::Dialect, DbError> {
    crate::query::parse_dialect(name)
        .map_err(|_| DbError::new(ErrorKind::Other, "unknown SQL dialect"))
}

/// The ids of every ids field of the read plan for `rows`, one statement per
/// field and 1000 rows. `None` when the rows or the scope cannot be resolved,
/// which sends the request to Python.
pub(crate) async fn read_ids<D: ReadDatabase>(
    plan: &CrudPlan,
    database: &D,
    rows: &[Row],
    caller: Option<CallerContext<'_>>,
) -> Result<Option<RowIds>, DbError> {
    if plan.read_links.is_empty() {
        return Ok(Some(RowIds::default()));
    }
    let owners: Option<Vec<Canon>> = rows
        .iter()
        .map(|row| plan.read_rows().key_of(row))
        .collect();
    let Some(owners) = owners else {
        return Ok(None);
    };
    let dialect = dialect_of(database.dialect())?;
    let mut slots = Vec::with_capacity(plan.read_links.len());
    for &index in &plan.read_links {
        let link = &plan.links[index];
        let Some(scope) = scope_of(link, caller) else {
            return Ok(None);
        };
        let mut found: HashMap<Canon, Vec<Canon>> = HashMap::new();
        for chunk in owners.chunks(CHUNK) {
            let (sql, values) = links::ids_select(&link.table, dialect, &scope, chunk)?;
            let fetched = database.fetch(&sql, &values).await?;
            found.extend(links::read_ids(&link.table, &fetched)?);
        }
        slots.push(found);
    }
    Ok(Some(RowIds::new(slots)))
}

/// A transaction read like a pool, so a written row is shown with the links
/// the same transaction wrote.
pub(crate) struct InTransaction<'a> {
    pub(crate) transaction: &'a Transaction,
    pub(crate) dialect: &'a str,
}

impl ReadDatabase for InTransaction<'_> {
    fn dialect(&self) -> &str {
        self.dialect
    }

    fn fetch<'a>(&'a self, sql: &'a str, values: &'a [Value]) -> FetchFuture<'a> {
        Box::pin(self.transaction.fetch(sql, values))
    }
}

/// How a link write ended when it did not set the links.
pub(crate) enum Refused {
    /// The owner row left the caller's scope while the write waited.
    OwnerMissing,
    /// The owner row was deleted while the write waited.
    OwnerGone,
    /// The ids field named targets that do not exist in scope.
    Unknown(String),
    /// Python answers (a limit the schema did not catch, an unreadable key).
    Replay,
}

/// Set the links of `owner` inside `transaction`. `owner_scope` holds the
/// conditions the owner row must still meet once its lock is taken.
pub(crate) async fn write_links(
    plan: &CrudPlan,
    dialect: &str,
    transaction: &Transaction,
    owner: &Value,
    (work, owner_scope): (&LinkWork, Scope),
) -> Result<Option<Refused>, DbError> {
    let dialect = dialect_of(dialect)?;
    for (index, keys) in &work.sets {
        let link = &plan.links[*index];
        let (Some(scope), Some(owner)) = (
            scope_of(link, work.caller_context()),
            link.table.owner_kind.read(owner),
        ) else {
            return Ok(Some(Refused::Replay));
        };
        let write = LinkWrite {
            op: LinkOp::Set,
            owner,
            keys: keys.clone(),
            scope,
            owner_scope: owner_scope.clone(),
        };
        let outcome = links::apply(
            transaction,
            dialect,
            &link.table,
            &write,
            &mut uuid::Uuid::new_v4,
        )
        .await?;
        match outcome {
            LinkOutcome::Done { .. } => {}
            LinkOutcome::OwnerMissing => return Ok(Some(Refused::OwnerMissing)),
            LinkOutcome::OwnerGone => return Ok(Some(Refused::OwnerGone)),
            LinkOutcome::Missing(_) => return Ok(Some(Refused::Unknown(link.field.clone()))),
            LinkOutcome::LimitExceeded => return Ok(Some(Refused::Replay)),
        }
    }
    Ok(None)
}
