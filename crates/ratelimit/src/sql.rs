//! The statements of the database store, built once per store. Every value is a
//! bind; only the table name is spliced in, and only after [`TableName`] checked it.

use hinoki_orm::query::Dialect;
use hinoki_orm::Value;

use crate::gcra::Step;
use crate::key::{Bucket, KeyError};

/// Expired buckets removed per sweep.
pub(crate) const SWEEP_BATCH: i64 = 500;

/// A table name that is safe to put in SQL: `^[a-z_][a-z0-9_]{0,62}$`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TableName(String);

impl TableName {
    pub fn new(name: &str) -> Result<Self, KeyError> {
        let mut bytes = name.bytes();
        let head_ok = bytes
            .next()
            .is_some_and(|byte| byte == b'_' || byte.is_ascii_lowercase());
        let rest_ok =
            bytes.all(|byte| byte == b'_' || byte.is_ascii_lowercase() || byte.is_ascii_digit());
        if head_ok && rest_ok && name.len() <= 63 {
            Ok(Self(name.to_owned()))
        } else {
            Err(KeyError::InvalidTable)
        }
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

#[derive(Debug)]
pub(crate) struct Statements {
    pub dialect: Dialect,
    pub hit: String,
    pub hit_existing: String,
    pub sweep: String,
    pub count: String,
}

pub(crate) fn dialect_of(name: &str) -> Dialect {
    match name {
        "postgres" => Dialect::Postgres,
        "sqlite" => Dialect::Sqlite,
        _ => Dialect::Mysql,
    }
}

pub(crate) fn statements(dialect: Dialect, table: &TableName) -> Statements {
    let t = table.as_str();
    match dialect {
        Dialect::Postgres => postgres(t),
        Dialect::Sqlite => sqlite(t),
        Dialect::Mysql => mysql(t),
    }
}

fn postgres(t: &str) -> Statements {
    let case = |now: u8, inc: u8, burst: u8| {
        format!(
            "CASE WHEN GREATEST(r.state / 2, ${now}) + ${inc} - ${now} <= ${burst} \
             THEN (GREATEST(r.state / 2, ${now}) + ${inc}) * 2 + 1 \
             ELSE (r.state / 2) * 2 END"
        )
    };
    Statements {
        dialect: Dialect::Postgres,
        hit: format!(
            "INSERT INTO {t} AS r (bucket, state) VALUES ($1, $2) \
             ON CONFLICT (bucket) DO UPDATE SET state = {} RETURNING state",
            case(3, 4, 5)
        ),
        hit_existing: format!(
            "UPDATE {t} AS r SET state = {} WHERE r.bucket = $1 RETURNING r.state",
            case(2, 3, 4)
        ),
        sweep: format!(
            "DELETE FROM {t} WHERE bucket IN \
             (SELECT bucket FROM {t} WHERE state < $1 ORDER BY state LIMIT $2)"
        ),
        count: format!("SELECT COUNT(*) FROM (SELECT 1 FROM {t} LIMIT $1) s"),
    }
}

fn sqlite(t: &str) -> Statements {
    let case = |now: u8, inc: u8, burst: u8| {
        format!(
            "CASE WHEN max(state / 2, ?{now}) + ?{inc} - ?{now} <= ?{burst} \
             THEN (max(state / 2, ?{now}) + ?{inc}) * 2 + 1 \
             ELSE (state / 2) * 2 END"
        )
    };
    Statements {
        dialect: Dialect::Sqlite,
        hit: format!(
            "INSERT INTO {t} (bucket, state) VALUES (?1, ?2) \
             ON CONFLICT (bucket) DO UPDATE SET state = {} RETURNING state",
            case(3, 4, 5)
        ),
        hit_existing: format!(
            "UPDATE {t} SET state = {} WHERE bucket = ?1 RETURNING state",
            case(2, 3, 4)
        ),
        sweep: format!(
            "DELETE FROM {t} WHERE bucket IN \
             (SELECT bucket FROM {t} WHERE state < ?1 ORDER BY state LIMIT ?2)"
        ),
        count: format!("SELECT COUNT(*) FROM (SELECT 1 FROM {t} LIMIT ?1) s"),
    }
}

fn mysql(t: &str) -> Statements {
    const CASE: &str = "IF(GREATEST(state DIV 2, ?) + ? - ? <= ?, \
                        (GREATEST(state DIV 2, ?) + ?) * 2 + 1, (state DIV 2) * 2)";
    Statements {
        dialect: Dialect::Mysql,
        hit: format!(
            "INSERT INTO {t} (bucket, state) VALUES (?, LAST_INSERT_ID(?)) \
             ON DUPLICATE KEY UPDATE state = LAST_INSERT_ID({CASE})"
        ),
        hit_existing: format!("UPDATE {t} SET state = LAST_INSERT_ID({CASE}) WHERE bucket = ?"),
        sweep: format!("DELETE FROM {t} WHERE state < ? ORDER BY state LIMIT ?"),
        count: format!("SELECT COUNT(*) FROM (SELECT 1 FROM {t} LIMIT ?) s"),
    }
}

impl Statements {
    /// The statement and binds of one hit; `existing` never inserts.
    pub(crate) fn hit_call(
        &self,
        bucket: &Bucket,
        step: &Step,
        existing: bool,
    ) -> (&str, Vec<Value>) {
        let key = Value::Text(bucket.hex_string());
        let (n, inc, burst) = (
            Value::Int(step.now_us),
            Value::Int(step.inc_us),
            Value::Int(step.burst_us),
        );
        match (self.dialect, existing) {
            (Dialect::Mysql, false) => (
                &self.hit,
                vec![
                    key,
                    Value::Int(step.fresh_state),
                    n.clone(),
                    inc.clone(),
                    n.clone(),
                    burst,
                    n,
                    inc,
                ],
            ),
            (Dialect::Mysql, true) => (
                &self.hit_existing,
                vec![n.clone(), inc.clone(), n.clone(), burst, n, inc, key],
            ),
            (_, false) => (
                &self.hit,
                vec![key, Value::Int(step.fresh_state), n, inc, burst],
            ),
            (_, true) => (&self.hit_existing, vec![key, n, inc, burst]),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_plain_lowercase_identifiers_are_table_names() {
        assert!(TableName::new("hinoki_rate_limits").is_ok());
        assert!(TableName::new("_t1").is_ok());
        for bad in [
            "",
            "1t",
            "Upper",
            "a-b",
            "a b",
            "t;drop",
            "a.b",
            &"x".repeat(64),
        ] {
            assert_eq!(TableName::new(bad), Err(KeyError::InvalidTable), "{bad}");
        }
    }
}
