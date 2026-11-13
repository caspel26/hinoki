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
}

impl Op {
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
}

impl fmt::Display for QueryError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::BadIdentifier(name) => write!(f, "invalid identifier {name:?}"),
            Self::BadFilter(why) => write!(f, "invalid filter: {why}"),
            Self::Empty(what) => write!(f, "{what} must not be empty"),
        }
    }
}

impl std::error::Error for QueryError {}

pub type Compiled<V> = Result<(String, Vec<V>), QueryError>;

/// `name` must be `[A-Za-z_][A-Za-z0-9_]*`; the result is double-quoted.
pub fn quote(name: &str) -> Result<String, QueryError> {
    let mut chars = name.chars();
    let valid = chars
        .next()
        .is_some_and(|c| c.is_ascii_alphabetic() || c == '_')
        && chars.all(|c| c.is_ascii_alphanumeric() || c == '_')
        && name.len() <= 63;
    if valid {
        Ok(format!("\"{name}\""))
    } else {
        Err(QueryError::BadIdentifier(name.to_owned()))
    }
}

/// A column reference: `name` (qualified with `base` when given) or
/// `alias.name` for a joined table.
fn column_ref(name: &str, base: Option<&str>) -> Result<String, QueryError> {
    match name.split_once('.') {
        Some((alias, column)) => Ok(format!("{}.{}", quote(alias)?, quote(column)?)),
        None => match base {
            Some(table) => Ok(format!("{}.{}", quote(table)?, quote(name)?)),
            None => quote(name),
        },
    }
}

fn quote_all(names: &[String]) -> Result<String, QueryError> {
    Ok(names
        .iter()
        .map(|n| quote(n))
        .collect::<Result<Vec<_>, _>>()?
        .join(", "))
}

struct Builder<V> {
    sql: String,
    params: Vec<V>,
    /// Table that qualifies bare column names (set when the query joins).
    base: Option<String>,
}

impl<V: Clone> Builder<V> {
    fn new(sql: String) -> Self {
        Self {
            sql,
            params: Vec::new(),
            base: None,
        }
    }

    fn placeholder(&mut self, value: V) -> String {
        self.params.push(value);
        format!("${}", self.params.len())
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
        let column = column_ref(&filter.column, self.base.as_deref())?;
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
            self.sql.push_str(" RETURNING ");
            self.sql.push_str(&quote_all(columns)?);
        }
        Ok(())
    }
}

fn select_list<V>(q: &Select<V>, joined: bool) -> Result<String, QueryError> {
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
        .map(|c| column_ref(c, base))
        .collect::<Result<Vec<_>, _>>()?;
    for join in &q.joins {
        for (index, column) in join.columns.iter().enumerate() {
            parts.push(format!(
                "{} AS {}",
                column_ref(&format!("{}.{column}", join.alias), None)?,
                quote(&format!("{}__{index}", join.alias))?
            ));
        }
    }
    Ok(parts.join(", "))
}

fn join_clauses<V>(q: &Select<V>) -> Result<String, QueryError> {
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
            quote(&join.table)?,
            quote(&join.alias)?,
            column_ref(&format!("{}.{}", join.alias, join.remote), None)?,
            column_ref(&join.local, Some(&q.table))?
        ));
    }
    Ok(sql)
}

pub fn compile_select<V: Clone>(q: &Select<V>) -> Compiled<V> {
    let table = quote(&q.table)?;
    let joined = !q.joins.is_empty();
    let columns = select_list(q, joined)?;
    let mut b = Builder::new(format!("SELECT {columns} FROM {table}"));
    b.sql.push_str(&join_clauses(q)?);
    if joined {
        b.base = Some(q.table.clone());
    }
    b.filters(&q.filters)?;
    if !q.order.is_empty() {
        let parts = q
            .order
            .iter()
            .map(|(c, desc)| {
                column_ref(c, b.base.as_deref()).map(|c| {
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
    if let Some(limit) = q.limit {
        b.sql.push_str(&format!(" LIMIT {limit}"));
    }
    if let Some(offset) = q.offset {
        b.sql.push_str(&format!(" OFFSET {offset}"));
    }
    Ok((b.sql, b.params))
}

pub fn compile_insert<V: Clone>(q: &Insert<V>) -> Compiled<V> {
    let table = quote(&q.table)?;
    if q.assignments.is_empty() {
        let mut b = Builder::new(format!("INSERT INTO {table} DEFAULT VALUES"));
        b.returning(&q.returning)?;
        return Ok((b.sql, b.params));
    }
    let names: Vec<String> = q.assignments.iter().map(|(c, _)| c.clone()).collect();
    let mut b = Builder::new(String::new());
    let holders: Vec<String> = q
        .assignments
        .iter()
        .map(|(_, v)| b.placeholder(v.clone()))
        .collect();
    b.sql = format!(
        "INSERT INTO {table} ({}) VALUES ({})",
        quote_all(&names)?,
        holders.join(", ")
    );
    b.returning(&q.returning)?;
    Ok((b.sql, b.params))
}

pub fn compile_update<V: Clone>(q: &Update<V>) -> Compiled<V> {
    let table = quote(&q.table)?;
    if q.assignments.is_empty() {
        return Err(QueryError::Empty("assignments"));
    }
    let mut b = Builder::new(String::new());
    let mut sets = Vec::new();
    for (column, value) in &q.assignments {
        sets.push(format!(
            "{} = {}",
            quote(column)?,
            b.placeholder(value.clone())
        ));
    }
    b.sql = format!("UPDATE {table} SET {}", sets.join(", "));
    b.filters(&q.filters)?;
    b.returning(&q.returning)?;
    Ok((b.sql, b.params))
}

pub fn compile_delete<V: Clone>(q: &Delete<V>) -> Compiled<V> {
    let mut b = Builder::new(format!("DELETE FROM {}", quote(&q.table)?));
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
}
