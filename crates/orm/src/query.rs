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

/// `JOIN table AS alias ON alias.remote = base.local`. The joined columns
/// are selected as `alias__N` (N = position in `columns`, so long column names
/// never overflow the identifier limit); an empty list joins only for filtering.
#[derive(Debug, Clone)]
pub struct Join {
    pub table: String,
    pub alias: String,
    pub local: String,
    pub remote: String,
    pub left: bool,
    pub columns: Vec<String>,
}

#[derive(Debug, Clone)]
pub struct Select<V> {
    pub table: String,
    pub columns: Vec<String>,
    pub joins: Vec<Join>,
    pub filters: Vec<Filter<V>>,
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
    pub returning: Vec<String>,
}

#[derive(Debug, Clone)]
pub struct Delete<V> {
    pub table: String,
    pub filters: Vec<Filter<V>>,
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

fn quote_with(name: &str, dialect: Dialect) -> Result<String, QueryError> {
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
fn column_ref(name: &str, base: Option<&str>, dialect: Dialect) -> Result<String, QueryError> {
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
            "LOWER(CAST({column} AS CHAR)) LIKE CONCAT('%', LOWER(REPLACE(REPLACE(REPLACE({holder}, '\\\\', '\\\\\\\\'), '%', '\\\\%'), '_', '\\\\_')), '%') ESCAPE '\\\\'"
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

struct Builder<V> {
    sql: String,
    params: Vec<V>,
    /// Table that qualifies bare column names (set when the query joins).
    base: Option<String>,
    dialect: Dialect,
}

impl<V: Clone> Builder<V> {
    fn new(sql: String, dialect: Dialect) -> Self {
        Self {
            sql,
            params: Vec::new(),
            base: None,
            dialect,
        }
    }

    fn placeholder(&mut self, value: V) -> String {
        self.params.push(value);
        match self.dialect {
            Dialect::Postgres => format!("${}", self.params.len()),
            Dialect::Sqlite => format!("?{}", self.params.len()),
            Dialect::Mysql => "?".to_owned(),
        }
    }

    fn filters(&mut self, filters: &[Filter<V>]) -> Result<(), QueryError> {
        let mut clauses = Vec::new();
        for filter in filters {
            clauses.push(self.clause(filter)?);
        }
        if !clauses.is_empty() {
            self.sql.push_str(" WHERE ");
            self.sql.push_str(&clauses.join(" AND "));
        }
        Ok(())
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
            Op::In => return self.membership(&column, filter),
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
        if filter.values.is_empty() {
            return Ok("FALSE".to_owned()); // nothing is a member of the empty set
        }
        let holders: Vec<String> = filter
            .values
            .iter()
            .map(|v| self.placeholder(v.clone()))
            .collect();
        Ok(format!("{column} IN ({})", holders.join(", ")))
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
    let mut sql = String::new();
    for join in &q.joins {
        if join.alias == q.table {
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
            column_ref(&join.local, Some(&q.table), dialect)?
        ));
    }
    Ok(sql)
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
    b.filters(&q.filters)?;
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
    b.filters(&q.filters)?;
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
    b.filters(&q.filters)?;
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
            returning: vec![],
        };
        let (sql, params) = compile_update(&update).unwrap();
        assert_eq!(sql, "UPDATE \"posts\" SET \"title\" = $1 WHERE \"id\" = $2");
        assert_eq!(params, vec![1, 9]);

        let delete = Delete {
            table: "posts".into(),
            filters: vec![f("id", Op::Ne, &[3])],
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
                + "'\\\\', '\\\\\\\\'), '%', '\\\\%'), '_', '\\\\_')), '%') ESCAPE '\\\\'"
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
}
