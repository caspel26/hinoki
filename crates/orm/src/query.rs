//! Query AST and SQL compilation.
//!
//! Values are never interpolated: every value becomes a `$n` placeholder and
//! is returned alongside the SQL in placeholder order. Identifiers (tables,
//! columns) must be plain names and are always quoted, so a hostile column
//! name cannot change the statement.

use std::fmt;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Op {
    Eq,
    Ne,
    Gt,
    Gte,
    Lt,
    Lte,
    In,
    /// `NOT IN`; an empty list excludes nothing.
    NotIn,
    IsNull,
    NotNull,
    Contains,
    /// `Contains` on any of several columns; `Filter::column` lists them
    /// separated by commas (a plain name cannot hold one).
    ContainsAny,
    /// `Contains` whose value is already the `LIKE` pattern (escaped and wrapped
    /// in `%` by [`like_pattern`]); never produced by [`Op::parse`].
    ContainsPattern,
    /// `ContainsAny` with an already built pattern.
    ContainsAnyPattern,
}

impl Op {
    fn is_pattern(self) -> bool {
        matches!(self, Self::ContainsPattern | Self::ContainsAnyPattern)
    }

    pub fn parse(text: &str) -> Option<Self> {
        Some(match text {
            "eq" => Self::Eq,
            "ne" => Self::Ne,
            "gt" => Self::Gt,
            "gte" => Self::Gte,
            "lt" => Self::Lt,
            "lte" => Self::Lte,
            "in" => Self::In,
            "isnull" => Self::IsNull,
            "notnull" => Self::NotNull,
            "contains" => Self::Contains,
            "containsany" => Self::ContainsAny,
            _ => return None,
        })
    }
}

#[derive(Debug, Clone)]
pub struct Filter<V> {
    pub column: String,
    pub op: Op,
    pub values: Vec<V>,
}

/// "Rows strictly after this position" for keyset pagination: `columns` are the
/// sort keys in order with their direction (`true` is descending), `values` the
/// position, one per column. Keys must not be NULL. The condition is
/// `(a, b) > (?, ?)` when every key sorts the same way and the expanded
/// `a > ? OR (a = ? AND b < ?)` form otherwise.
#[derive(Debug, Clone)]
pub struct Keyset<V> {
    pub columns: Vec<(String, bool)>,
    pub values: Vec<V>,
}

/// `JOIN table AS alias ON alias.remote = base.local`. The joined columns
/// are selected as `alias__N` (N = position in `columns`, so long column names
/// never overflow the identifier limit); an empty list joins only for filtering.
/// `live` names the joined table's soft-delete column: the join then matches
/// only rows where it `IS NULL`, so a soft-deleted row never shows through it.
#[derive(Debug, Clone)]
pub struct Join {
    pub table: String,
    pub alias: String,
    pub local: String,
    pub remote: String,
    pub left: bool,
    pub columns: Vec<String>,
    pub live: Option<String>,
}

/// `column [NOT] IN (SELECT ...)`: a condition on the outer statement that
/// compares `column` with the single column `select` returns. The inner select
/// shares the outer statement's placeholder numbering; it takes no ordering,
/// limit, offset or keyset, and nests at most [`MAX_SUBQUERY_DEPTH`] deep.
#[derive(Debug, Clone)]
pub struct SubFilter<V> {
    pub column: String,
    pub negated: bool,
    pub select: Box<Select<V>>,
}

/// Deepest nesting of subqueries: a many-to-many filter whose target is itself
/// filtered across a reverse relation.
pub const MAX_SUBQUERY_DEPTH: usize = 2;

#[derive(Debug, Clone)]
pub struct Select<V> {
    pub table: String,
    pub columns: Vec<String>,
    pub joins: Vec<Join>,
    pub filters: Vec<Filter<V>>,
    pub subfilters: Vec<SubFilter<V>>,
    /// Keyset (cursor) condition, ANDed with `filters`.
    pub keyset: Option<Keyset<V>>,
    pub order: Vec<(String, bool)>,
    pub limit: Option<u64>,
    pub offset: Option<u64>,
    /// `SELECT count(*)` instead of columns.
    pub count: bool,
}

#[derive(Debug, Clone)]
pub struct Insert<V> {
    pub table: String,
    pub assignments: Vec<(String, V)>,
    pub returning: Vec<String>,
}

#[derive(Debug, Clone)]
pub struct Update<V> {
    pub table: String,
    pub assignments: Vec<(String, V)>,
    pub filters: Vec<Filter<V>>,
    pub subfilters: Vec<SubFilter<V>>,
    pub returning: Vec<String>,
}

#[derive(Debug, Clone)]
pub struct Delete<V> {
    pub table: String,
    pub filters: Vec<Filter<V>>,
    pub subfilters: Vec<SubFilter<V>>,
    /// Columns of the deleted rows to return; not supported by MySQL.
    pub returning: Vec<String>,
}

impl<V> Filter<V> {
    /// The same filter with each value converted by `convert`.
    pub fn try_map<W, E>(
        self,
        convert: &mut impl FnMut(V) -> Result<W, E>,
    ) -> Result<Filter<W>, E> {
        Ok(Filter {
            column: self.column,
            op: self.op,
            values: self
                .values
                .into_iter()
                .map(&mut *convert)
                .collect::<Result<_, _>>()?,
        })
    }
}

impl<V> SubFilter<V> {
    /// The same subquery with each value converted by `convert`.
    pub fn try_map<W, E>(
        self,
        convert: &mut impl FnMut(V) -> Result<W, E>,
    ) -> Result<SubFilter<W>, E> {
        let select = *self.select;
        Ok(SubFilter {
            column: self.column,
            negated: self.negated,
            select: Box::new(Select {
                table: select.table,
                columns: select.columns,
                joins: select.joins,
                filters: map_filters(select.filters, convert)?,
                subfilters: map_subfilters(select.subfilters, convert)?,
                keyset: None,
                order: Vec::new(),
                limit: None,
                offset: None,
                count: false,
            }),
        })
    }
}

/// Every filter with its values converted by `convert`.
pub fn map_filters<V, W, E>(
    filters: Vec<Filter<V>>,
    convert: &mut impl FnMut(V) -> Result<W, E>,
) -> Result<Vec<Filter<W>>, E> {
    filters.into_iter().map(|f| f.try_map(convert)).collect()
}

/// Every subquery with its values converted by `convert`.
pub fn map_subfilters<V, W, E>(
    subfilters: Vec<SubFilter<V>>,
    convert: &mut impl FnMut(V) -> Result<W, E>,
) -> Result<Vec<SubFilter<W>>, E> {
    subfilters.into_iter().map(|f| f.try_map(convert)).collect()
}

#[derive(Debug, PartialEq, Eq)]
pub enum QueryError {
    BadIdentifier(String),
    BadFilter(String),
    Empty(&'static str),
    Unsupported(&'static str),
}

impl fmt::Display for QueryError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::BadIdentifier(name) => write!(f, "invalid identifier {name:?}"),
            Self::BadFilter(why) => write!(f, "invalid filter: {why}"),
            Self::Empty(what) => write!(f, "{what} must not be empty"),
            Self::Unsupported(what) => write!(f, "{what} is not supported by this dialect"),
        }
    }
}

impl std::error::Error for QueryError {}

pub type Compiled<V> = Result<(String, Vec<V>), QueryError>;

/// SQL behavior selected for one explicit database backend.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Dialect {
    #[default]
    Postgres,
    Sqlite,
    Mysql,
}

/// `name` must be `[A-Za-z_][A-Za-z0-9_]*`; the result is double-quoted.
pub fn quote(name: &str) -> Result<String, QueryError> {
    quote_with(name, Dialect::Postgres)
}

pub(crate) fn quote_with(name: &str, dialect: Dialect) -> Result<String, QueryError> {
    let mut chars = name.chars();
    let valid = chars
        .next()
        .is_some_and(|c| c.is_ascii_alphabetic() || c == '_')
        && chars.all(|c| c.is_ascii_alphanumeric() || c == '_')
        && name.len()
            <= match dialect {
                Dialect::Mysql => 64,
                Dialect::Postgres | Dialect::Sqlite => 63,
            };
    if valid {
        let delimiter = if dialect == Dialect::Mysql { '`' } else { '"' };
        Ok(format!("{delimiter}{name}{delimiter}"))
    } else {
        Err(QueryError::BadIdentifier(name.to_owned()))
    }
}

/// A column reference: `name` (qualified with `base` when given) or
/// `alias.name` for a joined table.
pub(crate) fn column_ref(
    name: &str,
    base: Option<&str>,
    dialect: Dialect,
) -> Result<String, QueryError> {
    match name.split_once('.') {
        Some((alias, column)) => Ok(format!(
            "{}.{}",
            quote_with(alias, dialect)?,
            quote_with(column, dialect)?
        )),
        None => match base {
            Some(table) => Ok(format!(
                "{}.{}",
                quote_with(table, dialect)?,
                quote_with(name, dialect)?
            )),
            None => quote_with(name, dialect),
        },
    }
}

/// The `LIKE` pattern for a substring match on `text`: `\\`, `%` and `_` escaped
/// with a backslash, wrapped in `%`. Used with the pattern operators so the
/// escaping is done once, not per row by the database.
pub fn like_pattern(text: &str) -> String {
    let mut pattern = String::with_capacity(text.len() + 2);
    pattern.push('%');
    for c in text.chars() {
        if matches!(c, '\\' | '%' | '_') {
            pattern.push('\\');
        }
        pattern.push(c);
    }
    pattern.push('%');
    pattern
}

fn like(column: &str, holder: &str, dialect: Dialect, prebuilt: bool) -> String {
    match (dialect, prebuilt) {
        (Dialect::Postgres, true) => format!("{column}::text ILIKE {holder}"),
        (Dialect::Sqlite, true) => {
            format!("CAST({column} AS TEXT) LIKE {holder} ESCAPE '\\'")
        }
        _ => like_escaping(column, holder, dialect),
    }
}

fn like_escaping(column: &str, holder: &str, dialect: Dialect) -> String {
    match dialect {
        Dialect::Postgres => format!(
            "{column}::text ILIKE '%' || replace(replace(replace({holder}::text, '\\', '\\\\'), '%', '\\%'), '_', '\\_') || '%'"
        ),
        Dialect::Sqlite => format!(
            "CAST({column} AS TEXT) LIKE '%' || replace(replace(replace({holder}, '\\', '\\\\'), '%', '\\%'), '_', '\\_') || '%' ESCAPE '\\'"
        ),
        Dialect::Mysql => format!(
            "LOWER(CAST({column} AS CHAR)) LIKE CONCAT('%', LOWER(REPLACE(REPLACE(REPLACE({holder}, '!', '!!'), '%', '!%'), '_', '!_')), '%') ESCAPE '!'"
        ),
    }
}

fn quote_all(names: &[String], dialect: Dialect) -> Result<String, QueryError> {
    Ok(names
        .iter()
        .map(|n| quote_with(n, dialect))
        .collect::<Result<Vec<_>, _>>()?
        .join(", "))
}

pub(crate) struct Builder<V> {
    pub(crate) sql: String,
    pub(crate) params: Vec<V>,
    /// Table that qualifies bare column names (set when the query joins).
    pub(crate) base: Option<String>,
    pub(crate) dialect: Dialect,
    /// Table an UPDATE or DELETE writes: MySQL refuses a subquery that reads
    /// it, so those subqueries are materialized.
    write_table: Option<String>,
    depth: usize,
}

impl<V: Clone> Builder<V> {
    pub(crate) fn new(sql: String, dialect: Dialect) -> Self {
        Self {
            sql,
            params: Vec::new(),
            base: None,
            dialect,
            write_table: None,
            depth: 0,
        }
    }

    pub(crate) fn placeholder(&mut self, value: V) -> String {
        self.params.push(value);
        match self.dialect {
            Dialect::Postgres => format!("${}", self.params.len()),
            Dialect::Sqlite => format!("?{}", self.params.len()),
            Dialect::Mysql => "?".to_owned(),
        }
    }

    fn filters(
        &mut self,
        filters: &[Filter<V>],
        subfilters: &[SubFilter<V>],
    ) -> Result<(), QueryError> {
        self.conditions(filters, None, subfilters)
    }

    fn conditions(
        &mut self,
        filters: &[Filter<V>],
        keyset: Option<&Keyset<V>>,
        subfilters: &[SubFilter<V>],
    ) -> Result<(), QueryError> {
        let clauses = self.clause_list(filters, keyset, subfilters)?;
        if !clauses.is_empty() {
            self.sql.push_str(" WHERE ");
            self.sql.push_str(&clauses.join(" AND "));
        }
        Ok(())
    }

    /// ` WHERE a AND b ...`, or nothing when there is no condition. Placeholders
    /// are numbered in the order the text is built.
    pub(crate) fn where_clause(
        &mut self,
        filters: &[Filter<V>],
        keyset: Option<&Keyset<V>>,
        subfilters: &[SubFilter<V>],
    ) -> Result<String, QueryError> {
        let clauses = self.clause_list(filters, keyset, subfilters)?;
        if clauses.is_empty() {
            return Ok(String::new());
        }
        Ok(format!(" WHERE {}", clauses.join(" AND ")))
    }

    fn clause_list(
        &mut self,
        filters: &[Filter<V>],
        keyset: Option<&Keyset<V>>,
        subfilters: &[SubFilter<V>],
    ) -> Result<Vec<String>, QueryError> {
        let mut clauses = Vec::new();
        for filter in filters {
            clauses.push(self.clause(filter)?);
        }
        if let Some(keyset) = keyset {
            clauses.push(self.keyset(keyset)?);
        }
        for sub in subfilters {
            clauses.push(self.subfilter(sub)?);
        }
        Ok(clauses)
    }

    fn subfilter(&mut self, sub: &SubFilter<V>) -> Result<String, QueryError> {
        if self.depth >= MAX_SUBQUERY_DEPTH {
            return Err(QueryError::Unsupported("subqueries nested this deeply"));
        }
        let column = column_ref(&sub.column, self.base.as_deref(), self.dialect)?;
        let mut inner = self.subselect(&sub.select)?;
        if self.reads_write_table(&sub.select) {
            inner = format!("SELECT * FROM ({inner}) AS hinoki_sub");
        }
        let word = if sub.negated { "NOT IN" } else { "IN" };
        Ok(format!("{column} {word} ({inner})"))
    }

    fn reads_write_table(&self, select: &Select<V>) -> bool {
        match &self.write_table {
            Some(table) if self.dialect == Dialect::Mysql => reads_table(select, table),
            _ => false,
        }
    }

    fn subselect(&mut self, q: &Select<V>) -> Result<String, QueryError> {
        let single = q.columns.len() == 1 && !q.count;
        if !single || q.keyset.is_some() || q.limit.is_some() || q.offset.is_some() {
            return Err(QueryError::BadFilter(
                "a subquery selects one column and takes no paging".into(),
            ));
        }
        let joined = !q.joins.is_empty();
        let base = joined.then_some(q.table.as_str());
        let column = column_ref(&q.columns[0], base, self.dialect)?;
        let mut sql = format!(
            "SELECT {column} FROM {}{}",
            quote_with(&q.table, self.dialect)?,
            join_clauses(q, self.dialect)?
        );
        let outer = self.base.take();
        self.base = joined.then(|| q.table.clone());
        self.depth += 1;
        let clause = self.where_clause(&q.filters, None, &q.subfilters);
        self.depth -= 1;
        self.base = outer;
        sql.push_str(&clause?);
        Ok(sql)
    }

    fn keyset(&mut self, keyset: &Keyset<V>) -> Result<String, QueryError> {
        let count = keyset.columns.len();
        if count == 0 || keyset.values.len() != count {
            return Err(QueryError::BadFilter(
                "a keyset needs one value per sort column".into(),
            ));
        }
        let mut columns = Vec::with_capacity(count);
        for (name, _) in &keyset.columns {
            columns.push(column_ref(name, self.base.as_deref(), self.dialect)?);
        }
        let descending = keyset.columns[0].1;
        let uniform = keyset.columns.iter().all(|(_, desc)| *desc == descending);
        if count == 1 || uniform {
            return Ok(self.row_comparison(&columns, &keyset.values, descending));
        }
        Ok(self.expanded_comparison(&columns, keyset))
    }

    fn row_comparison(&mut self, columns: &[String], values: &[V], descending: bool) -> String {
        let symbol = if descending { "<" } else { ">" };
        let holders: Vec<String> = values
            .iter()
            .map(|value| self.placeholder(value.clone()))
            .collect();
        if columns.len() == 1 {
            return format!("{} {symbol} {}", columns[0], holders[0]);
        }
        format!("({}) {symbol} ({})", columns.join(", "), holders.join(", "))
    }

    /// `k1 > v1 OR (k1 = v1 AND k2 < v2) OR ...`, each key in its own direction.
    /// A redundant `k1 >= v1` bound goes first: it gives the planner an index
    /// range to start from, which the OR alone does not.
    fn expanded_comparison(&mut self, columns: &[String], keyset: &Keyset<V>) -> String {
        let lead = if keyset.columns[0].1 { "<=" } else { ">=" };
        let bound = self.placeholder(keyset.values[0].clone());
        let mut branches = Vec::with_capacity(columns.len());
        for (index, (_, descending)) in keyset.columns.iter().enumerate() {
            let mut terms = Vec::with_capacity(index + 1);
            for (column, value) in columns.iter().zip(&keyset.values).take(index) {
                let holder = self.placeholder(value.clone());
                terms.push(format!("{column} = {holder}"));
            }
            let symbol = if *descending { "<" } else { ">" };
            let holder = self.placeholder(keyset.values[index].clone());
            terms.push(format!("{} {symbol} {holder}", columns[index]));
            branches.push(format!("({})", terms.join(" AND ")));
        }
        format!(
            "({} {lead} {bound} AND ({}))",
            columns[0],
            branches.join(" OR ")
        )
    }

    fn clause(&mut self, filter: &Filter<V>) -> Result<String, QueryError> {
        if matches!(filter.op, Op::ContainsAny | Op::ContainsAnyPattern) {
            return self.contains_any(filter);
        }
        let column = column_ref(&filter.column, self.base.as_deref(), self.dialect)?;
        let symbol = match filter.op {
            Op::Eq => "=",
            Op::Ne => "<>",
            Op::Gt => ">",
            Op::Gte => ">=",
            Op::Lt => "<",
            Op::Lte => "<=",
            Op::IsNull | Op::NotNull => {
                if !filter.values.is_empty() {
                    return Err(QueryError::BadFilter("isnull takes no values".into()));
                }
                let word = if filter.op == Op::IsNull {
                    "IS NULL"
                } else {
                    "IS NOT NULL"
                };
                return Ok(format!("{column} {word}"));
            }
            Op::In | Op::NotIn => return self.membership(&column, filter),
            Op::Contains | Op::ContainsAny | Op::ContainsPattern | Op::ContainsAnyPattern => {
                return self.contains(&column, filter)
            }
        };
        let [value] = filter.values.as_slice() else {
            return Err(QueryError::BadFilter(format!(
                "{} needs exactly one value",
                filter.column
            )));
        };
        Ok(format!(
            "{column} {symbol} {}",
            self.placeholder(value.clone())
        ))
    }

    /// Case-insensitive substring match; `\`, `%` and `_` in the value are
    /// escaped in SQL so they match literally.
    fn contains(&mut self, column: &str, filter: &Filter<V>) -> Result<String, QueryError> {
        let [value] = filter.values.as_slice() else {
            return Err(QueryError::BadFilter(format!(
                "{} needs exactly one value",
                filter.column
            )));
        };
        let holder = self.placeholder(value.clone());
        Ok(like(column, &holder, self.dialect, filter.op.is_pattern()))
    }

    /// One value matched against every listed column, joined with `OR`.
    fn contains_any(&mut self, filter: &Filter<V>) -> Result<String, QueryError> {
        let [value] = filter.values.as_slice() else {
            return Err(QueryError::BadFilter(format!(
                "{} needs exactly one value",
                filter.column
            )));
        };
        let mut columns = Vec::new();
        for name in filter.column.split(',') {
            columns.push(column_ref(name, self.base.as_deref(), self.dialect)?);
        }
        let shared_holder =
            (self.dialect != Dialect::Mysql).then(|| self.placeholder(value.clone()));
        let clauses: Vec<String> = columns
            .iter()
            .map(|column| {
                let holder = shared_holder
                    .clone()
                    .unwrap_or_else(|| self.placeholder(value.clone()));
                like(column, &holder, self.dialect, filter.op.is_pattern())
            })
            .collect();
        Ok(format!("({})", clauses.join(" OR ")))
    }

    fn membership(&mut self, column: &str, filter: &Filter<V>) -> Result<String, QueryError> {
        let negated = filter.op == Op::NotIn;
        if filter.values.is_empty() {
            // nothing is a member of the empty set, and everything is outside it
            return Ok(if negated { "TRUE" } else { "FALSE" }.to_owned());
        }
        let holders: Vec<String> = filter
            .values
            .iter()
            .map(|v| self.placeholder(v.clone()))
            .collect();
        let word = if negated { "NOT IN" } else { "IN" };
        Ok(format!("{column} {word} ({})", holders.join(", ")))
    }

    fn returning(&mut self, columns: &[String]) -> Result<(), QueryError> {
        if !columns.is_empty() {
            if self.dialect == Dialect::Mysql {
                return Err(QueryError::Unsupported("RETURNING"));
            }
            self.sql.push_str(" RETURNING ");
            self.sql.push_str(&quote_all(columns, self.dialect)?);
        }
        Ok(())
    }
}

fn select_list<V>(q: &Select<V>, joined: bool, dialect: Dialect) -> Result<String, QueryError> {
    if q.count {
        return Ok("count(*)".to_owned());
    }
    if q.columns.is_empty() {
        return Err(QueryError::Empty("columns"));
    }
    let base = joined.then_some(q.table.as_str());
    let mut parts = q
        .columns
        .iter()
        .map(|c| column_ref(c, base, dialect))
        .collect::<Result<Vec<_>, _>>()?;
    for join in &q.joins {
        for (index, column) in join.columns.iter().enumerate() {
            parts.push(format!(
                "{} AS {}",
                column_ref(&format!("{}.{column}", join.alias), None, dialect)?,
                quote_with(&format!("{}__{index}", join.alias), dialect)?
            ));
        }
    }
    Ok(parts.join(", "))
}

fn join_clauses<V>(q: &Select<V>, dialect: Dialect) -> Result<String, QueryError> {
    render_joins(&q.joins, &q.table, dialect)
}

/// The `JOIN` clauses of `joins` against the base table `table`.
pub(crate) fn render_joins(
    joins: &[Join],
    table: &str,
    dialect: Dialect,
) -> Result<String, QueryError> {
    let mut sql = String::new();
    for join in joins {
        if join.alias == table {
            return Err(QueryError::BadIdentifier(format!(
                "join alias {:?} equals the base table",
                join.alias
            )));
        }
        let kind = if join.left { "LEFT JOIN" } else { "INNER JOIN" };
        sql.push_str(&format!(
            " {kind} {} AS {} ON {} = {}",
            quote_with(&join.table, dialect)?,
            quote_with(&join.alias, dialect)?,
            column_ref(&format!("{}.{}", join.alias, join.remote), None, dialect)?,
            column_ref(&join.local, Some(table), dialect)?
        ));
        if let Some(live) = &join.live {
            sql.push_str(&format!(
                " AND {} IS NULL",
                column_ref(&format!("{}.{live}", join.alias), None, dialect)?
            ));
        }
    }
    Ok(sql)
}

/// Whether `select` reads `table` itself or through a join or a nested subquery.
fn reads_table<V>(select: &Select<V>, table: &str) -> bool {
    select.table == table
        || select.joins.iter().any(|join| join.table == table)
        || select
            .subfilters
            .iter()
            .any(|sub| reads_table(&sub.select, table))
}

pub fn compile_select<V: Clone>(q: &Select<V>) -> Compiled<V> {
    compile_select_with(q, Dialect::Postgres)
}

pub fn compile_select_with<V: Clone>(q: &Select<V>, dialect: Dialect) -> Compiled<V> {
    let table = quote_with(&q.table, dialect)?;
    let joined = !q.joins.is_empty();
    let mut columns = select_list(q, joined, dialect)?;
    // PostgreSQL names the column `count`; the other servers name it `count(*)`.
    if q.count && dialect != Dialect::Postgres {
        columns.push_str(" AS ");
        columns.push_str(&quote_with("count", dialect)?);
    }
    let mut b = Builder::new(format!("SELECT {columns} FROM {table}"), dialect);
    b.sql.push_str(&join_clauses(q, dialect)?);
    if joined {
        b.base = Some(q.table.clone());
    }
    b.conditions(&q.filters, q.keyset.as_ref(), &q.subfilters)?;
    if !q.order.is_empty() {
        let parts = q
            .order
            .iter()
            .map(|(c, desc)| {
                column_ref(c, b.base.as_deref(), dialect).map(|c| {
                    if *desc {
                        format!("{c} DESC")
                    } else {
                        format!("{c} ASC")
                    }
                })
            })
            .collect::<Result<Vec<_>, _>>()?;
        b.sql.push_str(&format!(" ORDER BY {}", parts.join(", ")));
    }
    b.sql.push_str(&limit_clause(dialect, q.limit, q.offset));
    Ok((b.sql, b.params))
}

/// `LIMIT` and `OFFSET`; SQLite and MySQL reject an `OFFSET` with no `LIMIT`, so
/// each gets its own "no limit" value.
fn limit_clause(dialect: Dialect, limit: Option<u64>, offset: Option<u64>) -> String {
    let mut clause = String::new();
    match (limit, offset, dialect) {
        (Some(limit), _, _) => clause.push_str(&format!(" LIMIT {limit}")),
        (None, Some(_), Dialect::Mysql) => clause.push_str(" LIMIT 18446744073709551615"),
        (None, Some(_), Dialect::Sqlite) => clause.push_str(" LIMIT -1"),
        _ => {}
    }
    if let Some(offset) = offset {
        clause.push_str(&format!(" OFFSET {offset}"));
    }
    clause
}

pub fn compile_insert<V: Clone>(q: &Insert<V>) -> Compiled<V> {
    compile_insert_with(q, Dialect::Postgres)
}

pub fn compile_insert_with<V: Clone>(q: &Insert<V>, dialect: Dialect) -> Compiled<V> {
    let table = quote_with(&q.table, dialect)?;
    if q.assignments.is_empty() {
        let mut b = Builder::new(format!("INSERT INTO {table} DEFAULT VALUES"), dialect);
        b.returning(&q.returning)?;
        return Ok((b.sql, b.params));
    }
    let names: Vec<String> = q.assignments.iter().map(|(c, _)| c.clone()).collect();
    let mut b = Builder::new(String::new(), dialect);
    let holders: Vec<String> = q
        .assignments
        .iter()
        .map(|(_, v)| b.placeholder(v.clone()))
        .collect();
    b.sql = format!(
        "INSERT INTO {table} ({}) VALUES ({})",
        quote_all(&names, dialect)?,
        holders.join(", ")
    );
    b.returning(&q.returning)?;
    Ok((b.sql, b.params))
}

pub fn compile_update<V: Clone>(q: &Update<V>) -> Compiled<V> {
    compile_update_with(q, Dialect::Postgres)
}

pub fn compile_update_with<V: Clone>(q: &Update<V>, dialect: Dialect) -> Compiled<V> {
    let table = quote_with(&q.table, dialect)?;
    if q.assignments.is_empty() {
        return Err(QueryError::Empty("assignments"));
    }
    let mut b = Builder::new(String::new(), dialect);
    let mut sets = Vec::new();
    for (column, value) in &q.assignments {
        sets.push(format!(
            "{} = {}",
            quote_with(column, dialect)?,
            b.placeholder(value.clone())
        ));
    }
    b.sql = format!("UPDATE {table} SET {}", sets.join(", "));
    if !q.subfilters.is_empty() {
        b.write_table = Some(q.table.clone());
    }
    b.filters(&q.filters, &q.subfilters)?;
    b.returning(&q.returning)?;
    Ok((b.sql, b.params))
}

pub fn compile_delete<V: Clone>(q: &Delete<V>) -> Compiled<V> {
    compile_delete_with(q, Dialect::Postgres)
}

pub fn compile_delete_with<V: Clone>(q: &Delete<V>, dialect: Dialect) -> Compiled<V> {
    let mut b = Builder::new(
        format!("DELETE FROM {}", quote_with(&q.table, dialect)?),
        dialect,
    );
    if !q.subfilters.is_empty() {
        b.write_table = Some(q.table.clone());
    }
    b.filters(&q.filters, &q.subfilters)?;
    if !q.returning.is_empty() {
        b.returning(&q.returning)?;
    }
    Ok((b.sql, b.params))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn f(column: &str, op: Op, values: &[i32]) -> Filter<i32> {
        Filter {
            column: column.into(),
            op,
            values: values.to_vec(),
        }
    }

    fn select(filters: Vec<Filter<i32>>) -> Select<i32> {
        Select {
            table: "posts".into(),
            columns: vec!["id".into(), "title".into()],
            joins: vec![],
            filters,
            subfilters: vec![],
            keyset: None,
            order: vec![],
            limit: None,
            offset: None,
            count: false,
        }
    }

    #[test]
    fn selects_with_filters_order_and_paging() {
        let mut q = select(vec![f("published", Op::Eq, &[1]), f("n", Op::Gte, &[5])]);
        q.order = vec![("created_at".into(), true), ("id".into(), false)];
        q.limit = Some(10);
        q.offset = Some(20);
        let (sql, params) = compile_select(&q).unwrap();
        assert_eq!(
            sql,
            "SELECT \"id\", \"title\" FROM \"posts\" WHERE \"published\" = $1 AND \"n\" >= $2 \
             ORDER BY \"created_at\" DESC, \"id\" ASC LIMIT 10 OFFSET 20"
        );
        assert_eq!(params, vec![1, 5]);
    }

    #[test]
    fn membership_expands_and_the_empty_set_matches_nothing() {
        let (sql, params) = compile_select(&select(vec![f("id", Op::In, &[7, 8, 9])])).unwrap();
        assert!(sql.ends_with("WHERE \"id\" IN ($1, $2, $3)"));
        assert_eq!(params, vec![7, 8, 9]);
        let (sql, params) = compile_select(&select(vec![f("id", Op::In, &[])])).unwrap();
        assert!(sql.ends_with("WHERE FALSE"));
        assert!(params.is_empty());
    }

    fn keyset(columns: &[(&str, bool)], values: &[i32]) -> Keyset<i32> {
        Keyset {
            columns: columns.iter().map(|(c, d)| ((*c).into(), *d)).collect(),
            values: values.to_vec(),
        }
    }

    #[test]
    fn keyset_on_one_key_is_a_plain_comparison() {
        let mut q = select(vec![f("published", Op::Eq, &[1])]);
        q.keyset = Some(keyset(&[("id", false)], &[7]));
        let (sql, params) = compile_select(&q).unwrap();
        assert!(
            sql.ends_with("WHERE \"published\" = $1 AND \"id\" > $2"),
            "{sql}"
        );
        assert_eq!(params, vec![1, 7]);
        q.keyset = Some(keyset(&[("id", true)], &[7]));
        assert!(compile_select(&q).unwrap().0.ends_with("\"id\" < $2"));
    }

    #[test]
    fn keyset_with_uniform_direction_uses_a_row_comparison() {
        let mut q = select(vec![]);
        q.keyset = Some(keyset(&[("created", true), ("id", true)], &[5, 9]));
        let (sql, params) = compile_select_with(&q, Dialect::Sqlite).unwrap();
        assert!(
            sql.ends_with("WHERE (\"created\", \"id\") < (?1, ?2)"),
            "{sql}"
        );
        assert_eq!(params, vec![5, 9]);
        let (sql, _) = compile_select_with(&q, Dialect::Mysql).unwrap();
        assert!(sql.ends_with("WHERE (`created`, `id`) < (?, ?)"), "{sql}");
    }

    #[test]
    fn keyset_with_mixed_directions_expands_each_branch() {
        let mut q = select(vec![]);
        q.keyset = Some(keyset(
            &[("a", false), ("b", true), ("id", false)],
            &[1, 2, 3],
        ));
        let (sql, params) = compile_select(&q).unwrap();
        assert!(
            sql.ends_with(
                "WHERE (\"a\" >= $1 AND ((\"a\" > $2) OR (\"a\" = $3 AND \"b\" < $4) \
                 OR (\"a\" = $5 AND \"b\" = $6 AND \"id\" > $7)))"
            ),
            "{sql}"
        );
        assert_eq!(params, vec![1, 1, 1, 2, 1, 2, 3]);
        let (sql, params) = compile_select_with(&q, Dialect::Mysql).unwrap();
        assert_eq!(sql.matches('?').count(), 7);
        assert_eq!(params, vec![1, 1, 1, 2, 1, 2, 3]);
    }

    #[test]
    fn keyset_qualifies_columns_of_a_joined_query_and_checks_its_shape() {
        let mut q = select(vec![]);
        q.joins = vec![Join {
            table: "authors".into(),
            alias: "rel_author".into(),
            local: "author_id".into(),
            remote: "id".into(),
            left: false,
            columns: vec![],
            live: None,
        }];
        q.keyset = Some(keyset(&[("id", false)], &[1]));
        assert!(compile_select(&q)
            .unwrap()
            .0
            .contains("\"posts\".\"id\" > $1"));
        q.keyset = Some(keyset(&[("id", false)], &[]));
        assert!(compile_select(&q).is_err());
        q.keyset = Some(keyset(&[], &[]));
        assert!(compile_select(&q).is_err());
        q.keyset = Some(keyset(&[("id; DROP", false)], &[1]));
        assert!(compile_select(&q).is_err());
    }

    #[test]
    fn null_checks_take_no_values() {
        let (sql, _) = compile_select(&select(vec![f("deleted", Op::IsNull, &[])])).unwrap();
        assert!(sql.ends_with("\"deleted\" IS NULL"));
        assert!(compile_select(&select(vec![f("deleted", Op::IsNull, &[1])])).is_err());
    }

    #[test]
    fn joins_qualify_columns_and_alias_joined_ones() {
        let mut q = select(vec![f("author.name", Op::Eq, &[1]), f("id", Op::Ne, &[2])]);
        q.joins = vec![Join {
            table: "users".into(),
            alias: "author".into(),
            local: "author_id".into(),
            remote: "id".into(),
            left: false,
            columns: vec!["id".into(), "name".into()],
            live: None,
        }];
        q.order = vec![("author.name".into(), false), ("title".into(), true)];
        let (sql, params) = compile_select(&q).unwrap();
        assert_eq!(
            sql,
            "SELECT \"posts\".\"id\", \"posts\".\"title\", \"author\".\"id\" AS \"author__0\", \
             \"author\".\"name\" AS \"author__1\" FROM \"posts\" INNER JOIN \"users\" AS \"author\" \
             ON \"author\".\"id\" = \"posts\".\"author_id\" WHERE \"author\".\"name\" = $1 AND \
             \"posts\".\"id\" <> $2 ORDER BY \"author\".\"name\" ASC, \"posts\".\"title\" DESC"
        );
        assert_eq!(params, vec![1, 2]);
        q.joins[0].left = true;
        q.joins[0].alias = "posts".into();
        assert!(compile_select(&q).is_err());
        q.joins[0].alias = "a\"; DROP".into();
        assert!(compile_select(&q).is_err());
    }

    #[test]
    fn a_live_join_matches_only_rows_that_are_not_soft_deleted() {
        let mut q = select(vec![]);
        q.joins = vec![Join {
            table: "users".into(),
            alias: "author".into(),
            local: "author_id".into(),
            remote: "id".into(),
            left: true,
            columns: vec![],
            live: Some("deleted_at".into()),
        }];
        let (sql, _) = compile_select_with(&q, Dialect::Mysql).unwrap();
        assert!(sql.contains(
            "LEFT JOIN `users` AS `author` ON `author`.`id` = `posts`.`author_id` \
             AND `author`.`deleted_at` IS NULL"
        ));
        q.joins[0].live = Some("x; DROP".into());
        assert!(compile_select(&q).is_err());
    }

    #[test]
    fn counts_without_columns() {
        let mut q = select(vec![]);
        q.count = true;
        q.columns.clear();
        assert_eq!(
            compile_select(&q).unwrap().0,
            "SELECT count(*) FROM \"posts\""
        );
    }

    #[test]
    fn names_the_count_column_on_every_dialect() {
        let mut q = select(vec![]);
        q.count = true;
        q.columns.clear();
        for (dialect, expected) in [
            (Dialect::Postgres, "SELECT count(*) FROM \"posts\""),
            (
                Dialect::Sqlite,
                "SELECT count(*) AS \"count\" FROM \"posts\"",
            ),
            (Dialect::Mysql, "SELECT count(*) AS `count` FROM `posts`"),
        ] {
            assert_eq!(compile_select_with(&q, dialect).unwrap().0, expected);
        }
    }

    #[test]
    fn inserts_updates_and_deletes_use_placeholders_only() {
        let insert = Insert {
            table: "posts".into(),
            assignments: vec![("title".into(), 1), ("slug".into(), 2)],
            returning: vec!["id".into()],
        };
        let (sql, params) = compile_insert(&insert).unwrap();
        assert_eq!(
            sql,
            "INSERT INTO \"posts\" (\"title\", \"slug\") VALUES ($1, $2) RETURNING \"id\""
        );
        assert_eq!(params, vec![1, 2]);

        let update = Update {
            table: "posts".into(),
            assignments: vec![("title".into(), 1)],
            filters: vec![f("id", Op::Eq, &[9])],
            subfilters: vec![],
            returning: vec![],
        };
        let (sql, params) = compile_update(&update).unwrap();
        assert_eq!(sql, "UPDATE \"posts\" SET \"title\" = $1 WHERE \"id\" = $2");
        assert_eq!(params, vec![1, 9]);

        let delete = Delete {
            table: "posts".into(),
            filters: vec![f("id", Op::Ne, &[3])],
            subfilters: vec![],
            returning: vec![],
        };
        assert_eq!(
            compile_delete(&delete).unwrap().0,
            "DELETE FROM \"posts\" WHERE \"id\" <> $1"
        );
    }

    #[test]
    fn sqlite_dialect_uses_numbered_question_mark_holders() {
        let query = select(vec![f("title", Op::Eq, &[1]), f("id", Op::In, &[2, 3])]);
        let (sql, params) = compile_select_with(&query, Dialect::Sqlite).unwrap();
        assert_eq!(
            sql,
            "SELECT \"id\", \"title\" FROM \"posts\" WHERE \"title\" = ?1 AND \"id\" IN (?2, ?3)"
        );
        assert_eq!(params, vec![1, 2, 3]);

        let insert = Insert {
            table: "posts".into(),
            assignments: vec![("title".into(), 1)],
            returning: vec!["id".into()],
        };
        assert_eq!(
            compile_insert_with(&insert, Dialect::Sqlite).unwrap().0,
            "INSERT INTO \"posts\" (\"title\") VALUES (?1) RETURNING \"id\""
        );
    }

    #[test]
    fn sqlite_contains_uses_cast_like_and_explicit_escape() {
        let query = select(vec![f("title", Op::Contains, &[1])]);
        let sql = compile_select_with(&query, Dialect::Sqlite).unwrap().0;
        assert!(sql.contains("CAST(\"title\" AS TEXT) LIKE"));
        assert!(sql.ends_with("ESCAPE '\\'"));
    }

    #[test]
    fn mysql_uses_backticks_positional_holders_and_concat() {
        let query = select(vec![
            f("title", Op::Eq, &[1]),
            f("body", Op::Contains, &[2]),
        ]);
        let (sql, params) = compile_select_with(&query, Dialect::Mysql).unwrap();
        assert_eq!(
            sql,
            "SELECT `id`, `title` FROM `posts` WHERE `title` = ? AND ".to_owned()
                + "LOWER(CAST(`body` AS CHAR)) LIKE CONCAT('%', LOWER(REPLACE(REPLACE(REPLACE(?, "
                + "'!', '!!'), '%', '!%'), '_', '!_')), '%') ESCAPE '!'"
        );
        assert_eq!(params, vec![1, 2]);

        let insert = Insert {
            table: "posts".into(),
            assignments: vec![("title".into(), 1)],
            returning: vec!["id".into()],
        };
        assert_eq!(
            compile_insert_with(&insert, Dialect::Mysql),
            Err(QueryError::Unsupported("RETURNING"))
        );
    }

    #[test]
    fn mysql_contains_any_allocates_one_placeholder_per_column() {
        let query = Select {
            table: "posts".into(),
            columns: vec!["id".into()],
            joins: vec![],
            filters: vec![Filter {
                column: "title,body".into(),
                op: Op::ContainsAny,
                values: vec!["needle".to_owned()],
            }],
            subfilters: vec![],
            keyset: None,
            order: vec![],
            limit: None,
            offset: None,
            count: false,
        };
        let (sql, params) = compile_select_with(&query, Dialect::Mysql).unwrap();
        assert!(sql.contains("LOWER(CAST(`title` AS CHAR)) LIKE CONCAT('%', LOWER("));
        assert!(sql.contains(" OR LOWER(CAST(`body` AS CHAR)) LIKE CONCAT('%', LOWER("));
        assert_eq!(sql.matches('?').count(), 2);
        assert_eq!(params, vec!["needle", "needle"]);
    }

    #[test]
    fn mysql_contains_does_not_depend_on_backslash_escapes() {
        let sql = like_escaping("title", "?", Dialect::Mysql);
        assert!(sql.ends_with("ESCAPE '!'"), "{sql}");
        assert!(!sql.contains('\\'), "{sql}");
    }

    #[test]
    fn mysql_offset_without_limit_uses_the_maximum_limit() {
        let mut query = select(vec![]);
        query.offset = Some(4);
        let (sql, params) = compile_select_with(&query, Dialect::Mysql).unwrap();
        assert!(sql.ends_with(" LIMIT 18446744073709551615 OFFSET 4"));
        assert!(params.is_empty());
    }

    #[test]
    fn sqlite_offset_without_limit_uses_an_unbounded_limit() {
        let mut query = select(vec![]);
        query.offset = Some(4);
        let (sql, _) = compile_select_with(&query, Dialect::Sqlite).unwrap();
        assert!(sql.ends_with(" LIMIT -1 OFFSET 4"));
    }

    #[test]
    fn hostile_identifiers_are_rejected_before_any_sql_is_built() {
        for bad in ["a\"; DROP TABLE x; --", "a b", "", "1abc", "naïve", "a-b"] {
            assert!(
                matches!(quote(bad), Err(QueryError::BadIdentifier(_))),
                "{bad}"
            );
        }
        assert!(compile_select(&select(vec![f("id\" OR \"1\"=\"1", Op::Eq, &[1])])).is_err());
        let mut q = select(vec![]);
        q.table = "posts; DROP TABLE posts".into();
        assert!(compile_select(&q).is_err());
    }

    #[test]
    fn updates_need_assignments_and_comparisons_need_one_value() {
        let update = Update::<i32> {
            table: "t".into(),
            assignments: vec![],
            filters: vec![],
            subfilters: vec![],
            returning: vec![],
        };
        assert_eq!(
            compile_update(&update),
            Err(QueryError::Empty("assignments"))
        );
        assert!(compile_select(&select(vec![f("n", Op::Eq, &[1, 2])])).is_err());
    }

    #[test]
    fn pattern_contains_binds_a_built_pattern_without_sql_escaping() {
        let postgres = select(vec![f("title", Op::ContainsPattern, &[1])]);
        let (sql, params) = compile_select(&postgres).unwrap();
        assert!(sql.ends_with("WHERE \"title\"::text ILIKE $1"), "{sql}");
        assert_eq!(params, vec![1]);
        let sqlite = compile_select_with(&postgres, Dialect::Sqlite).unwrap().0;
        assert!(
            sqlite.ends_with("CAST(\"title\" AS TEXT) LIKE ?1 ESCAPE '\\'"),
            "{sqlite}"
        );
        let any = select(vec![f("title,body", Op::ContainsAnyPattern, &[1])]);
        let (sql, params) = compile_select(&any).unwrap();
        assert!(
            sql.ends_with("(\"title\"::text ILIKE $1 OR \"body\"::text ILIKE $1)"),
            "{sql}"
        );
        assert_eq!(params, vec![1]);
    }

    #[test]
    fn like_pattern_escapes_wildcards_and_wraps() {
        assert_eq!(like_pattern("abc"), "%abc%");
        assert_eq!(like_pattern(""), "%%");
        assert_eq!(like_pattern("50%_\\x"), "%50\\%\\_\\\\x%");
        assert_eq!(like_pattern("é_"), "%é\\_%");
    }

    #[test]
    fn contains_escapes_like_wildcards_in_sql() {
        let (sql, params) = compile_select(&select(vec![f("title", Op::Contains, &[1])])).unwrap();
        assert!(sql.contains("\"title\"::text ILIKE '%' || replace(replace(replace($1::text,"));
        assert_eq!(params, vec![1]);
        assert!(compile_select(&select(vec![f("title", Op::Contains, &[1, 2])])).is_err());
    }

    #[test]
    fn contains_any_matches_each_column_with_one_parameter() {
        let filter = f("title,body", Op::ContainsAny, &[1]);
        let (sql, params) = compile_select(&select(vec![filter])).unwrap();
        assert!(sql.contains("WHERE (\"title\"::text ILIKE "));
        assert!(sql.contains(" OR \"body\"::text ILIKE '%' || replace(replace(replace($1::text,"));
        assert_eq!(params, vec![1]);
        assert!(compile_select(&select(vec![f("title,", Op::ContainsAny, &[1])])).is_err());
        assert!(compile_select(&select(vec![f("a,b", Op::ContainsAny, &[1, 2])])).is_err());
    }

    fn bridge_sub(owner: i32, filters: Vec<Filter<i32>>) -> SubFilter<i32> {
        let mut inner = select(vec![f("post_id", Op::Eq, &[owner])]);
        inner.table = "posts_tags".into();
        inner.columns = vec!["tag_id".into()];
        inner.filters.extend(filters);
        SubFilter {
            column: "id".into(),
            negated: false,
            select: Box::new(inner),
        }
    }

    #[test]
    fn subquery_shares_the_placeholder_numbering_in_text_order() {
        let mut q = select(vec![f("n", Op::Gt, &[1])]);
        q.subfilters = vec![bridge_sub(2, vec![]), {
            let mut sub = bridge_sub(3, vec![]);
            sub.negated = true;
            sub
        }];
        q.filters.push(f("title", Op::Eq, &[4]));
        let (sql, params) = compile_select(&q).unwrap();
        assert_eq!(
            sql,
            "SELECT \"id\", \"title\" FROM \"posts\" WHERE \"n\" > $1 AND \"title\" = $2 \
             AND \"id\" IN (SELECT \"tag_id\" FROM \"posts_tags\" WHERE \"post_id\" = $3) \
             AND \"id\" NOT IN (SELECT \"tag_id\" FROM \"posts_tags\" WHERE \"post_id\" = $4)"
        );
        assert_eq!(params, vec![1, 4, 2, 3]);
        let (sqlite, _) = compile_select_with(&q, Dialect::Sqlite).unwrap();
        assert!(sqlite.contains("\"post_id\" = ?3)"));
    }

    #[test]
    fn subquery_with_joins_qualifies_only_its_own_columns() {
        let mut inner = select(vec![f("tags.name", Op::Eq, &[1])]);
        inner.table = "posts_tags".into();
        inner.columns = vec!["post_id".into()];
        inner.joins = vec![Join {
            table: "tags".into(),
            alias: "tags".into(),
            local: "tag_id".into(),
            remote: "id".into(),
            left: false,
            columns: vec![],
            live: None,
        }];
        let mut q = select(vec![]);
        q.subfilters = vec![SubFilter {
            column: "id".into(),
            negated: false,
            select: Box::new(inner),
        }];
        let (sql, _) = compile_select(&q).unwrap();
        assert!(sql.ends_with(
            "\"id\" IN (SELECT \"posts_tags\".\"post_id\" FROM \"posts_tags\" \
             INNER JOIN \"tags\" AS \"tags\" ON \"tags\".\"id\" = \"posts_tags\".\"tag_id\" \
             WHERE \"tags\".\"name\" = $1)"
        ));
    }

    #[test]
    fn subquery_rejects_paging_extra_columns_and_deep_nesting() {
        let mut paged = bridge_sub(1, vec![]);
        paged.select.limit = Some(1);
        let mut wide = bridge_sub(1, vec![]);
        wide.select.columns.push("other".into());
        for sub in [paged, wide] {
            let mut q = select(vec![]);
            q.subfilters = vec![sub];
            assert!(compile_select(&q).is_err());
        }
        let mut deep = bridge_sub(1, vec![]);
        for _ in 0..MAX_SUBQUERY_DEPTH {
            let mut outer = bridge_sub(1, vec![]);
            outer.select.subfilters = vec![deep];
            deep = outer;
        }
        let mut q = select(vec![]);
        q.subfilters = vec![deep];
        assert!(compile_select(&q).is_err());
    }

    #[test]
    fn not_in_with_no_values_excludes_nothing() {
        let (sql, _) = compile_select(&select(vec![f("id", Op::NotIn, &[])])).unwrap();
        assert!(sql.ends_with("WHERE TRUE"));
        let (sql, params) = compile_select(&select(vec![f("id", Op::NotIn, &[1, 2])])).unwrap();
        assert!(sql.ends_with("\"id\" NOT IN ($1, $2)"));
        assert_eq!(params, vec![1, 2]);
    }

    #[test]
    fn mysql_materializes_a_subquery_that_reads_the_written_table() {
        let mut sub = bridge_sub(1, vec![]);
        sub.select.table = "posts".into();
        let delete = Delete {
            table: "posts".into(),
            filters: vec![],
            subfilters: vec![sub],
            returning: vec![],
        };
        let (sql, _) = compile_delete_with(&delete, Dialect::Mysql).unwrap();
        assert!(sql.contains("IN (SELECT * FROM (SELECT `tag_id` FROM `posts`"));
        assert!(sql.ends_with("AS hinoki_sub)"));
        let (other, _) = compile_delete_with(
            &Delete {
                subfilters: vec![bridge_sub(1, vec![])],
                ..delete
            },
            Dialect::Mysql,
        )
        .unwrap();
        assert!(!other.contains("hinoki_sub"));
    }

    #[test]
    fn delete_returning_is_refused_on_mysql_only() {
        let delete = Delete::<i32> {
            table: "posts_tags".into(),
            filters: vec![f("post_id", Op::Eq, &[1])],
            subfilters: vec![],
            returning: vec!["tag_id".into()],
        };
        assert!(compile_delete(&delete)
            .unwrap()
            .0
            .ends_with("RETURNING \"tag_id\""));
        assert!(compile_delete_with(&delete, Dialect::Mysql).is_err());
    }
}
