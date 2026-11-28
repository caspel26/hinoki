//! Zero-copy PostgreSQL row container for direct wire-buffer JSON encoding.
//!
//! Holds the raw `tokio_postgres::Row` values (backed by reference-counted
//! network `Bytes` buffers) so projected JSON responses can borrow `&str` and
//! `&[u8]` directly without allocating per-cell `String`s or `Vec<Value>`s.
//! If Python later materializes the rows, [`PgRows::to_row_set`] decodes them
//! on demand.

use std::sync::Arc;

use tokio_postgres::types::{FromSql, Type};
use tokio_postgres::Row as PgRow;

use crate::database::{decode_values, PgNumeric, PgTimestamp};
use crate::error::{DbError, ErrorKind};
use crate::links::{Canon, KeyKind};
use crate::value::Value;
use crate::RowSet;

/// A borrowed or stack-decoded cell read straight from a `tokio_postgres::Row`.
#[derive(Debug)]
pub enum PgCell<'a> {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Text(&'a str),
    Bytes(&'a [u8]),
    Uuid(uuid::Uuid),
    Timestamp(time::OffsetDateTime),
    Numeric(String),
}

struct RawLen(usize);

impl<'a> FromSql<'a> for RawLen {
    fn from_sql(
        _ty: &Type,
        raw: &'a [u8],
    ) -> Result<Self, Box<dyn std::error::Error + Sync + Send>> {
        Ok(Self(raw.len()))
    }

    fn accepts(_ty: &Type) -> bool {
        true
    }
}

/// Borrowed handle to one row in a [`PgRows`] batch, caching its column metadata.
#[derive(Clone, Copy)]
pub struct PgRowRef<'a> {
    row: &'a PgRow,
    columns: &'a [tokio_postgres::Column],
}

impl<'a> PgRowRef<'a> {
    /// Borrow or stack-decode one cell directly from the Postgres wire buffer.
    pub fn cell(self, col_idx: usize) -> Option<PgCell<'a>> {
        let col = self.columns.get(col_idx)?;
        decode_pg_cell(self.row, col_idx, col.type_())
    }

    /// Read a key column as a [`Canon`] without allocating a `Value`.
    pub fn canon_key(self, col_idx: usize, kind: KeyKind) -> Option<Canon> {
        match (kind, self.cell(col_idx)?) {
            (KeyKind::Int, PgCell::Int(n)) => Some(Canon::Int(n)),
            (KeyKind::Text { .. }, PgCell::Text(s)) => Some(Canon::Text(s.to_owned())),
            (KeyKind::Uuid, PgCell::Uuid(id)) => Some(Canon::Uuid(id)),
            _ => None,
        }
    }

    /// Estimated JSON byte length of a text/decimal cell without UTF-8 validation.
    pub fn text_len(self, col_idx: usize) -> usize {
        self.row
            .try_get::<_, Option<RawLen>>(col_idx)
            .ok()
            .flatten()
            .map_or(0, |r| r.0.saturating_add(2))
    }

    /// Estimated base64 JSON byte length of a `bytea` cell (0 if null).
    pub fn bytes_len(self, col_idx: usize) -> usize {
        self.row
            .try_get::<_, Option<RawLen>>(col_idx)
            .ok()
            .flatten()
            .map_or(0, |r| r.0.div_ceil(3).saturating_mul(4).saturating_add(2))
    }
}

/// Raw PostgreSQL rows plus cached column types.
#[derive(Debug, Default)]
pub struct PgRows {
    rows: Vec<PgRow>,
}

impl PgRows {
    pub(crate) fn new(rows: Vec<PgRow>) -> Self {
        Self { rows }
    }

    pub fn is_empty(&self) -> bool {
        self.rows.is_empty()
    }

    pub fn len(&self) -> usize {
        self.rows.len()
    }

    pub fn extend_from(&mut self, other: Self) {
        self.rows.extend(other.rows);
    }

    /// Check that the wire columns match `expected` names in order.
    pub fn columns_match(&self, expected: &[String]) -> bool {
        let Some(first) = self.rows.first() else {
            return true;
        };
        let cols = first.columns();
        cols.len() == expected.len()
            && cols
                .iter()
                .zip(expected)
                .all(|(col, name)| col.name() == name.as_str())
    }

    /// Number of columns in each row (0 if empty).
    pub fn column_count(&self) -> usize {
        self.rows.first().map_or(0, |r| r.columns().len())
    }

    /// Borrow one row by index.
    pub fn row(&self, row_idx: usize) -> Option<PgRowRef<'_>> {
        let row = self.rows.get(row_idx)?;
        Some(PgRowRef {
            row,
            columns: row.columns(),
        })
    }

    /// Iterate over all rows as borrowed [`PgRowRef`] handles.
    pub fn iter(&self) -> impl ExactSizeIterator<Item = PgRowRef<'_>> {
        self.rows.iter().map(|row| PgRowRef {
            row,
            columns: row.columns(),
        })
    }

    /// Borrow or stack-decode one cell directly from the Postgres wire buffer.
    pub fn cell(&self, row_idx: usize, col_idx: usize) -> Option<PgCell<'_>> {
        self.row(row_idx)?.cell(col_idx)
    }

    /// Read a key column as a [`Canon`] without allocating a `Value`.
    pub fn canon_key(&self, row_idx: usize, col_idx: usize, kind: KeyKind) -> Option<Canon> {
        self.row(row_idx)?.canon_key(col_idx, kind)
    }

    /// Read a key column as an owned [`Value`] (for binding into a prefetch `IN (...)`).
    pub fn key_value(
        &self,
        row_idx: usize,
        col_idx: usize,
        kind: KeyKind,
    ) -> Option<(Canon, Value)> {
        match (kind, self.cell(row_idx, col_idx)?) {
            (KeyKind::Int, PgCell::Int(n)) => Some((Canon::Int(n), Value::Int(n))),
            (KeyKind::Text { .. }, PgCell::Text(s)) => {
                let owned = s.to_owned();
                Some((Canon::Text(owned.clone()), Value::Text(owned)))
            }
            (KeyKind::Uuid, PgCell::Uuid(id)) => Some((Canon::Uuid(id), Value::Uuid(id))),
            _ => None,
        }
    }

    /// Materialize into an owned [`RowSet`] (used only when Python inspects rows
    /// or on fallback paths).
    pub fn to_row_set(&self) -> Result<RowSet, DbError> {
        let Some(first) = self.rows.first() else {
            return Ok(RowSet::default());
        };
        let cols = first.columns();
        let columns: Arc<[String]> = cols.iter().map(|c| c.name().to_owned()).collect();
        let types: Vec<&Type> = cols.iter().map(|c| c.type_()).collect();
        let rows = self
            .rows
            .iter()
            .map(|row| decode_values(row, &types))
            .collect::<Result<Vec<_>, _>>()?;
        Ok(RowSet { columns, rows })
    }
}

fn pg_get<'a, T: FromSql<'a>>(row: &'a PgRow, idx: usize) -> Option<Option<T>> {
    row.try_get(idx).ok()
}

fn decode_pg_cell<'a>(row: &'a PgRow, idx: usize, ty: &Type) -> Option<PgCell<'a>> {
    macro_rules! cell {
        ($t:ty, $variant:expr) => {
            pg_get::<$t>(row, idx)?.map_or(PgCell::Null, $variant)
        };
    }
    Some(match *ty {
        Type::VOID => PgCell::Null,
        Type::BOOL => cell!(bool, PgCell::Bool),
        Type::INT2 => cell!(i16, |v| PgCell::Int(i64::from(v))),
        Type::INT4 => cell!(i32, |v| PgCell::Int(i64::from(v))),
        Type::INT8 => cell!(i64, PgCell::Int),
        Type::FLOAT4 => cell!(f32, |v| PgCell::Float(f64::from(v))),
        Type::FLOAT8 => cell!(f64, PgCell::Float),
        Type::TEXT | Type::VARCHAR | Type::BPCHAR | Type::NAME => cell!(&'a str, PgCell::Text),
        Type::BYTEA => cell!(&'a [u8], PgCell::Bytes),
        Type::UUID => cell!(uuid::Uuid, PgCell::Uuid),
        Type::TIMESTAMPTZ => cell!(PgTimestamp, |v: PgTimestamp| PgCell::Timestamp(v.0)),
        Type::NUMERIC => cell!(PgNumeric, |v: PgNumeric| PgCell::Numeric(v.0)),
        _ => return None,
    })
}

/// Either zero-copy PostgreSQL rows or an already-decoded [`RowSet`].
#[derive(Debug)]
pub enum RawRows {
    Postgres(PgRows),
    Decoded(RowSet),
}

impl RawRows {
    pub fn len(&self) -> usize {
        match self {
            Self::Postgres(pg) => pg.len(),
            Self::Decoded(set) => set.len(),
        }
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    pub fn to_row_set(&self) -> Result<RowSet, DbError> {
        match self {
            Self::Postgres(pg) => pg.to_row_set(),
            Self::Decoded(set) => Ok(set.clone()),
        }
    }
}

pub(crate) fn extract_pg_count(rows: &PgRows) -> Result<i64, DbError> {
    match rows.cell(0, 0) {
        Some(PgCell::Int(count)) => Ok(count),
        _ => Err(DbError::new(
            ErrorKind::Type,
            "expected an integer count from the count query",
        )),
    }
}
