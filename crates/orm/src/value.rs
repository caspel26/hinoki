//! Values exchanged with PostgreSQL and their wire encoding.

use std::error::Error;

use bytes::BytesMut;
use time::format_description::well_known::Rfc3339;
use time::{OffsetDateTime, UtcOffset};
use tokio_postgres::types::{to_sql_checked, IsNull, Json, ToSql, Type};
use uuid::Uuid;

use crate::numeric;

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
            Type::NUMERIC => Self::encode_numeric(&i.to_string(), out),
            _ => Err(Value::Int(i).mismatch(ty)),
        }
    }

    fn encode_numeric(
        text: &str,
        out: &mut BytesMut,
    ) -> Result<IsNull, Box<dyn Error + Sync + Send>> {
        let mut raw = Vec::new();
        numeric::encode(text, &mut raw).map_err(|e| e.0)?;
        out.extend_from_slice(&raw);
        Ok(IsNull::No)
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
            (Self::Text(s), &Type::NUMERIC) => Self::encode_numeric(s, out),
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

#[inline]
fn push_2(out: &mut Vec<u8>, n: u8) {
    out.push(b'0' + (n / 10));
    out.push(b'0' + (n % 10));
}

/// Write ISO 8601 text with microseconds when nonzero and a `+00:00` offset
/// directly into `out` without intermediate heap allocations.
pub fn write_timestamp_iso(out: &mut Vec<u8>, value: &OffsetDateTime) {
    let value = value.to_offset(UtcOffset::UTC);
    let year = value.year();
    out.reserve(32);
    if (0..=9999).contains(&year) {
        let y = year as u16;
        push_2(out, (y / 100) as u8);
        push_2(out, (y % 100) as u8);
    } else {
        use std::io::Write;
        let _ = write!(out, "{year:04}");
    }
    out.push(b'-');
    push_2(out, u8::from(value.month()));
    out.push(b'-');
    push_2(out, value.day());
    out.push(b'T');
    push_2(out, value.hour());
    out.push(b':');
    push_2(out, value.minute());
    out.push(b':');
    push_2(out, value.second());
    let micros = value.microsecond();
    if micros != 0 {
        out.push(b'.');
        push_2(out, (micros / 10_000) as u8);
        push_2(out, ((micros / 100) % 100) as u8);
        push_2(out, (micros % 100) as u8);
    }
    out.extend_from_slice(b"+00:00");
}

/// ISO 8601 text with microseconds when nonzero and a `+00:00` offset, the
/// form Python's `datetime.fromisoformat` and Hinoki's JSON output use.
pub fn timestamp_iso(value: &OffsetDateTime) -> String {
    let mut out = Vec::with_capacity(32);
    write_timestamp_iso(&mut out, value);
    String::from_utf8(out).expect("ISO 8601 timestamp is ASCII")
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

#[cfg(test)]
mod timestamp_tests {
    use super::{parse_timestamp, timestamp_iso, timestamp_storage_text};
    use time::macros::datetime;

    #[test]
    fn storage_text_keeps_six_fraction_digits_and_z() {
        let value = datetime!(2026-11-16 10:11:12.123 UTC);
        assert_eq!(
            timestamp_storage_text(&value).as_deref(),
            Some("2026-11-16T10:11:12.123000Z")
        );
        let whole = datetime!(0001-01-01 0:00 UTC);
        assert_eq!(
            timestamp_storage_text(&whole).as_deref(),
            Some("0001-01-01T00:00:00.000000Z")
        );
        assert!(timestamp_storage_text(&datetime!(-0001-12-31 0:00 UTC)).is_none());
        assert!(timestamp_storage_text(&datetime!(+10000-01-01 0:00 UTC)).is_none());
    }

    #[test]
    fn iso_text_uses_micros_only_when_needed_and_a_utc_offset() {
        let value = datetime!(2026-11-16 12:11:12.000_5 +02:00);
        assert_eq!(timestamp_iso(&value), "2026-11-16T10:11:12.000500+00:00");
        let whole = datetime!(9999-12-31 23:59:59 UTC);
        assert_eq!(timestamp_iso(&whole), "9999-12-31T23:59:59+00:00");
    }

    #[test]
    fn write_timestamp_iso_matches_reference_across_offsets_and_edge_dates() {
        use super::write_timestamp_iso;
        use time::UtcOffset;

        fn reference_iso(value: &time::OffsetDateTime) -> String {
            let v = value.to_offset(UtcOffset::UTC);
            let mut s = format!(
                "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}",
                v.year(),
                u8::from(v.month()),
                v.day(),
                v.hour(),
                v.minute(),
                v.second()
            );
            let micros = v.microsecond();
            if micros != 0 {
                s.push_str(&format!(".{micros:06}"));
            }
            s.push_str("+00:00");
            s
        }

        let cases = [
            datetime!(2026-11-16 10:11:12 UTC),
            datetime!(2026-11-16 10:11:12.000_001 UTC),
            datetime!(2026-11-16 10:11:12.999_999 UTC),
            datetime!(2026-11-16 12:11:12.123_456 +05:30),
            datetime!(2026-01-01 00:30:00 -04:00),
            datetime!(2024-02-29 23:59:59.5 +02:00),
            datetime!(2000-02-29 00:00:00 UTC),
            datetime!(0001-01-01 00:00:00 UTC),
            datetime!(0000-01-01 00:00:00 UTC),
            datetime!(0042-07-04 03:04:05.007_008 UTC),
            datetime!(9999-12-31 23:59:59.999_999 UTC),
            datetime!(-0001-12-31 23:59:59 UTC),
            datetime!(-0044-03-15 12:00:00.5 UTC),
            datetime!(+10000-01-01 00:00:00 UTC),
        ];

        for dt in cases {
            let mut buf = Vec::new();
            write_timestamp_iso(&mut buf, &dt);
            let actual = String::from_utf8(buf).unwrap();
            assert_eq!(actual, reference_iso(&dt), "mismatch for {dt}");
            assert_eq!(
                timestamp_iso(&dt),
                actual,
                "timestamp_iso mismatch for {dt}"
            );
        }
    }

    #[test]
    fn parsing_normalizes_offsets_and_rejects_invalid_text() {
        let parsed = parse_timestamp("2026-11-16T12:11:12.5+02:00").unwrap();
        assert_eq!(parsed, datetime!(2026-11-16 10:11:12.5 UTC));
        assert_eq!(parsed.offset(), time::UtcOffset::UTC);
        let stored = timestamp_storage_text(&parsed).unwrap();
        assert_eq!(parse_timestamp(&stored), Some(parsed));
        // `time` has no leap seconds: a month-end one becomes the last instant
        // of its minute, and any other second 60 is refused.
        assert_eq!(
            parse_timestamp("2026-12-31T23:59:60Z"),
            Some(datetime!(2026-12-31 23:59:59.999_999_999 UTC))
        );
        assert!(parse_timestamp("2026-11-16T10:11:60Z").is_none());
        assert!(parse_timestamp("2026-11-16 10:11:12").is_none());
        assert!(parse_timestamp("not a timestamp").is_none());
    }
}
