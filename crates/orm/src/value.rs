//! Values exchanged with PostgreSQL and their wire encoding.

use std::error::Error;

use bytes::BytesMut;
use time::format_description::well_known::Rfc3339;
use time::{OffsetDateTime, UtcOffset};
use tokio_postgres::types::{to_sql_checked, IsNull, Json, ToSql, Type};
use uuid::Uuid;

/// A database value. Parameters are sent in binary according to the type the
/// server inferred for the placeholder; a mismatch is an error, never a cast.
#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Text(String),
    Bytes(Vec<u8>),
    Uuid(Uuid),
    /// Always normalized to UTC.
    Timestamp(OffsetDateTime),
    Json(serde_json::Value),
}

impl Value {
    /// A UTC timestamp value for any instant.
    pub fn timestamp(value: OffsetDateTime) -> Self {
        Self::Timestamp(value.to_offset(UtcOffset::UTC))
    }

    fn kind(&self) -> &'static str {
        match self {
            Self::Null => "null",
            Self::Bool(_) => "bool",
            Self::Int(_) => "int",
            Self::Float(_) => "float",
            Self::Text(_) => "text",
            Self::Bytes(_) => "bytes",
            Self::Uuid(_) => "uuid",
            Self::Timestamp(_) => "timestamp",
            Self::Json(_) => "json",
        }
    }

    fn mismatch(&self, ty: &Type) -> Box<dyn Error + Sync + Send> {
        format!("cannot send a {} value as {}", self.kind(), ty.name()).into()
    }

    fn encode_int(
        i: i64,
        ty: &Type,
        out: &mut BytesMut,
    ) -> Result<IsNull, Box<dyn Error + Sync + Send>> {
        match *ty {
            Type::INT2 => i16::try_from(i)?.to_sql(ty, out),
            Type::INT4 => i32::try_from(i)?.to_sql(ty, out),
            Type::INT8 => i.to_sql(ty, out),
            Type::FLOAT8 => (i as f64).to_sql(ty, out),
            _ => Err(Value::Int(i).mismatch(ty)),
        }
    }
}

impl ToSql for Value {
    fn to_sql(
        &self,
        ty: &Type,
        out: &mut BytesMut,
    ) -> Result<IsNull, Box<dyn Error + Sync + Send>> {
        match (self, ty) {
            (Self::Null, _) => Ok(IsNull::Yes),
            (Self::Bool(b), &Type::BOOL) => b.to_sql(ty, out),
            (Self::Int(i), _) => Self::encode_int(*i, ty, out),
            (Self::Float(f), &Type::FLOAT8) => f.to_sql(ty, out),
            (Self::Float(f), &Type::FLOAT4) => (*f as f32).to_sql(ty, out),
            (Self::Text(s), &(Type::TEXT | Type::VARCHAR | Type::BPCHAR | Type::NAME)) => {
                s.to_sql(ty, out)
            }
            (Self::Bytes(b), &Type::BYTEA) => b.to_sql(ty, out),
            (Self::Uuid(u), &Type::UUID) => u.to_sql(ty, out),
            (Self::Timestamp(t), &Type::TIMESTAMPTZ) => t.to_sql(ty, out),
            (Self::Json(v), &(Type::JSON | Type::JSONB)) => Json(v).to_sql(ty, out),
            _ => Err(self.mismatch(ty)),
        }
    }

    fn accepts(_ty: &Type) -> bool {
        true
    }

    to_sql_checked!();
}

/// Parse RFC 3339 text (any offset) into a UTC instant.
pub fn parse_timestamp(text: &str) -> Option<OffsetDateTime> {
    OffsetDateTime::parse(text, &Rfc3339)
        .ok()
        .map(|value| value.to_offset(UtcOffset::UTC))
}

/// ISO 8601 text with microseconds when nonzero and a `+00:00` offset, the
/// form Python's `datetime.fromisoformat` and Hinoki's JSON output use.
pub fn timestamp_iso(value: &OffsetDateTime) -> String {
    let value = value.to_offset(UtcOffset::UTC);
    let mut text = format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}",
        value.year(),
        u8::from(value.month()),
        value.day(),
        value.hour(),
        value.minute(),
        value.second()
    );
    let micros = value.microsecond();
    if micros != 0 {
        text.push_str(&format!(".{micros:06}"));
    }
    text.push_str("+00:00");
    text
}

/// The fixed RFC 3339 text Hinoki stores in SQLite: six fractional digits, `Z`.
/// RFC 3339 has four-digit years only, so other years have no text form.
pub fn timestamp_storage_text(value: &OffsetDateTime) -> Option<String> {
    let value = value.to_offset(UtcOffset::UTC);
    if !(0..=9999).contains(&value.year()) {
        return None;
    }
    Some(format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}.{:06}Z",
        value.year(),
        u8::from(value.month()),
        value.day(),
        value.hour(),
        value.minute(),
        value.second(),
        value.microsecond()
    ))
}

