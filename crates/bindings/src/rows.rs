//! Rust-only row projection and JSON encoding for eligible generated CRUD pages.

use hinoki_orm::Value;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::sync::Arc;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Kind {
    Text,
    Int,
    Float,
    Bool,
    Uuid,
    Timestamp,
    Bytes,
}

impl Kind {
    fn parse(value: &str) -> PyResult<Self> {
        match value {
            "str" => Ok(Self::Text),
            "int" => Ok(Self::Int),
            "float" => Ok(Self::Float),
            "bool" => Ok(Self::Bool),
            "uuid" => Ok(Self::Uuid),
            "datetime" => Ok(Self::Timestamp),
            "bytes" => Ok(Self::Bytes),
            _ => Err(PyValueError::new_err(format!(
                "unsupported row wire kind {value:?}"
            ))),
        }
    }
}

#[derive(Clone, Debug)]
struct Field {
    column_index: usize,
    wire_name: String,
    kind: Kind,
    nullable: bool,
}

/// Metadata only: this plan owns no Python objects and is safe in a Tokio task.
#[derive(Clone, Debug)]
pub(crate) struct RowPlan {
    columns: Vec<String>,
    fields: Vec<Field>,
}

pub(crate) enum PageWireResult {
    Encoded(Vec<u8>),
    Fallback(Vec<Vec<(String, Value)>>),
}

#[pyclass(name = "_RowPlan", module = "hinoki.core")]
pub(crate) struct PyRowPlan {
    pub(crate) inner: Arc<RowPlan>,
}

#[pymethods]
impl PyRowPlan {
    /// `columns` is the complete SQL projection; fields are in Schema order.
    #[new]
    fn new(columns: Vec<String>, fields: Vec<(String, String, String, bool)>) -> PyResult<Self> {
        RowPlan::new(columns, fields).map(|inner| Self {
            inner: Arc::new(inner),
        })
    }
}

impl RowPlan {
    pub(crate) fn new(
        columns: Vec<String>,
        fields: Vec<(String, String, String, bool)>,
    ) -> PyResult<Self> {
        let mut planned = Vec::with_capacity(fields.len());
        let mut names = std::collections::HashSet::with_capacity(columns.len());
        for column in &columns {
            if !names.insert(column) {
                return Err(PyValueError::new_err("duplicate RowPlan SQL column"));
            }
        }
        let mut wire_names = std::collections::HashSet::with_capacity(fields.len());
        for (name, wire_name, kind, nullable) in fields {
            if !wire_names.insert(wire_name.clone()) {
                return Err(PyValueError::new_err("duplicate RowPlan wire name"));
            }
            let column_index = columns
                .iter()
                .position(|column| column == &name)
                .ok_or_else(|| PyValueError::new_err(format!("unknown row column {name:?}")))?;
            planned.push(Field {
                column_index,
                wire_name,
                kind: Kind::parse(&kind)?,
                nullable,
            });
        }
        Ok(Self {
            columns,
            fields: planned,
        })
    }
}

pub(crate) fn encode_page(
    rows: &[Vec<(String, Value)>],
    plan: &RowPlan,
    page: u64,
    page_size: u64,
    total: u64,
) -> Option<Vec<u8>> {
    if page == 0 || page_size == 0 {
        return None;
    }
    let mut output = Vec::new();
    output.push(b'{');
    write_key(&mut output, "items")?;
    output.push(b'[');
    for (index, row) in rows.iter().enumerate() {
        if index > 0 {
            output.push(b',');
        }
        write_row(&mut output, row, plan)?;
    }
    output.extend_from_slice(b"],\"page\":");
    write_number(&mut output, page)?;
    output.extend_from_slice(b",\"page_size\":");
    write_number(&mut output, page_size)?;
    output.extend_from_slice(b",\"total\":");
    write_number(&mut output, total)?;
    output.extend_from_slice(b",\"pages\":");
    write_number(&mut output, total.div_ceil(page_size))?;
    output.push(b'}');
    Some(output)
}

pub(crate) fn encode_cursor_page(
    rows: &[Vec<(String, Value)>],
    plan: &RowPlan,
    links: &crate::cursor::Links,
    page_size: u64,
) -> Option<Vec<u8>> {
    if page_size == 0 {
        return None;
    }
    let mut output = Vec::new();
    output.push(b'{');
    write_key(&mut output, "items")?;
    output.push(b'[');
    for (index, row) in rows.iter().enumerate() {
        if index > 0 {
            output.push(b',');
        }
        write_row(&mut output, row, plan)?;
    }
    output.extend_from_slice(b"],\"next\":");
    write_link(&mut output, links.next.as_deref());
    output.extend_from_slice(b",\"previous\":");
    write_link(&mut output, links.previous.as_deref());
    output.extend_from_slice(b",\"page_size\":");
    write_number(&mut output, page_size)?;
    output.push(b'}');
    Some(output)
}

/// A cursor is URL-safe base64, so it needs no JSON escaping.
fn write_link(output: &mut Vec<u8>, link: Option<&str>) {
    match link {
        Some(token) => {
            output.push(b'"');
            output.extend_from_slice(token.as_bytes());
            output.push(b'"');
        }
        None => output.extend_from_slice(b"null"),
    }
}

pub(crate) fn encode_item(row: &[(String, Value)], plan: &RowPlan) -> Option<Vec<u8>> {
    let mut output = Vec::new();
    write_row(&mut output, row, plan)?;
    Some(output)
}

fn write_row(output: &mut Vec<u8>, row: &[(String, Value)], plan: &RowPlan) -> Option<()> {
    validate_columns(row, &plan.columns)?;
    output.push(b'{');
    for (index, field) in plan.fields.iter().enumerate() {
        if index > 0 {
            output.push(b',');
        }
        write_key(output, &field.wire_name)?;
        write_value(output, &row[field.column_index].1, field)?;
    }
    output.push(b'}');
    Some(())
}

fn validate_columns(row: &[(String, Value)], columns: &[String]) -> Option<()> {
    if row.len() != columns.len() {
        return None;
    }
    for (index, (name, _)) in row.iter().enumerate() {
        if name != &columns[index] {
            return None;
        }
    }
    if row.iter().any(|(_, value)| {
        matches!(value, Value::Timestamp(timestamp) if !python_datetime_representable(timestamp))
    }) {
        return None;
    }
    Some(())
}

fn python_datetime_representable(value: &time::OffsetDateTime) -> bool {
    (1..=9999).contains(&value.year())
}

fn write_value(output: &mut Vec<u8>, value: &Value, field: &Field) -> Option<()> {
    if matches!(value, Value::Null) {
        if field.nullable {
            output.extend_from_slice(b"null");
            return Some(());
        }
        return None;
    }
    match (field.kind, value) {
        (Kind::Text, Value::Text(value)) => write_json(output, value),
        (Kind::Int, Value::Int(value)) => write_number(output, value),
        (Kind::Float, Value::Float(value)) => write_float(output, *value),
        (Kind::Bool, Value::Bool(value)) => {
            output.extend_from_slice(if *value { b"true" } else { b"false" });
            Some(())
        }
        (Kind::Uuid, Value::Uuid(value)) => write_json(output, &value.to_string()),
        (Kind::Timestamp, Value::Timestamp(value)) => {
            if !(1..=9999).contains(&value.year()) {
                return None;
            }
            write_json(output, &hinoki_orm::timestamp_iso(value))
        }
        (Kind::Bytes, Value::Bytes(value)) => {
            use base64::{engine::general_purpose::STANDARD, Engine};
            write_json(output, &STANDARD.encode(value))
        }
        _ => None,
    }
}

fn write_key(output: &mut Vec<u8>, value: &str) -> Option<()> {
    write_json(output, value)?;
    output.push(b':');
    Some(())
}

fn write_json(output: &mut Vec<u8>, value: &str) -> Option<()> {
    serde_json::to_writer(output, value).ok()?;
    Some(())
}

fn write_number<T: serde::Serialize>(output: &mut Vec<u8>, value: T) -> Option<()> {
    serde_json::to_writer(output, &value).ok()?;
    Some(())
}

fn write_float(output: &mut Vec<u8>, value: f64) -> Option<()> {
    let number = serde_json::Number::from_f64(value)?;
    output.extend_from_slice(number.to_string().as_bytes());
    Some(())
}

#[cfg(test)]
mod tests {
    use super::{encode_item, encode_page, RowPlan};
    use hinoki_orm::Value;
    use time::macros::datetime;
    use uuid::Uuid;

    fn plan() -> RowPlan {
        RowPlan::new(
            vec![
                "id".into(),
                "name".into(),
                "created".into(),
                "weight".into(),
            ],
            vec![
                ("id".into(), "key".into(), "uuid".into(), false),
                ("name".into(), "displayName".into(), "str".into(), true),
                ("created".into(), "created".into(), "datetime".into(), false),
                ("weight".into(), "weight".into(), "float".into(), false),
            ],
        )
        .unwrap()
    }

    #[test]
    fn encodes_one_item_with_schema_alias_order() {
        let plan = RowPlan::new(
            vec!["id".to_owned(), "display_name".to_owned()],
            vec![
                (
                    "display_name".to_owned(),
                    "displayName".to_owned(),
                    "str".to_owned(),
                    false,
                ),
                ("id".to_owned(), "id".to_owned(), "int".to_owned(), false),
            ],
        )
        .unwrap();
        let row = vec![
            ("id".to_owned(), Value::Int(9)),
            (
                "display_name".to_owned(),
                Value::Text("Hinoki 🌲".to_owned()),
            ),
        ];

        assert_eq!(
            encode_item(&row, &plan).unwrap(),
            r#"{"displayName":"Hinoki 🌲","id":9}"#.as_bytes()
        );
    }

    fn row(name: Value, weight: Value) -> Vec<(String, Value)> {
        vec![
            (
                "id".into(),
                Value::Uuid(Uuid::parse_str("7f9c4a5e-3c1d-4a2b-9b0d-1e2f3a4b5c6d").unwrap()),
            ),
            ("name".into(), name),
            (
                "created".into(),
                Value::Timestamp(datetime!(2026-11-16 10:00:00 UTC)),
            ),
            ("weight".into(), weight),
        ]
    }

    #[test]
    fn encodes_alias_order_scalars_and_page_envelope() {
        let bytes = encode_page(
            &[row(Value::Text("Hinoki\n🌲".into()), Value::Float(-0.0))],
            &plan(),
            2,
            10,
            11,
        )
        .unwrap();
        assert_eq!(
            bytes,
            r#"{"items":[{"key":"7f9c4a5e-3c1d-4a2b-9b0d-1e2f3a4b5c6d","displayName":"Hinoki\n🌲","created":"2026-11-16T10:00:00+00:00","weight":-0.0}],"page":2,"page_size":10,"total":11,"pages":2}"#.as_bytes()
        );
    }

    #[test]
    fn encodes_the_cursor_envelope_with_links_or_null() {
        let links = crate::cursor::Links {
            next: Some("abc-_".into()),
            previous: None,
        };
        let bytes = super::encode_cursor_page(
            &[row(Value::Text("a".into()), Value::Float(1.5))],
            &plan(),
            &links,
            10,
        )
        .unwrap();
        assert_eq!(
            bytes,
            r#"{"items":[{"key":"7f9c4a5e-3c1d-4a2b-9b0d-1e2f3a4b5c6d","displayName":"a","created":"2026-11-16T10:00:00+00:00","weight":1.5}],"next":"abc-_","previous":null,"page_size":10}"#.as_bytes()
        );
        let none = crate::cursor::Links {
            next: None,
            previous: None,
        };
        let empty = super::encode_cursor_page(&[], &plan(), &none, 3).unwrap();
        assert_eq!(
            empty,
            br#"{"items":[],"next":null,"previous":null,"page_size":3}"#
        );
        assert!(super::encode_cursor_page(&[], &plan(), &none, 0).is_none());
    }

    #[test]
    fn encodes_null_large_integer_and_fractional_timestamp() {
        let mut planned = plan();
        planned.fields = vec![
            planned.fields[1].clone(),
            super::Field {
                column_index: 2,
                wire_name: "created".into(),
                kind: super::Kind::Timestamp,
                nullable: false,
            },
            super::Field {
                column_index: 3,
                wire_name: "count".into(),
                kind: super::Kind::Int,
                nullable: false,
            },
        ];
        let mut values = row(Value::Null, Value::Int(i64::MAX));
        let timestamp = datetime!(2026-11-16 10:00:00 UTC) + time::Duration::microseconds(123_000);
        values[2].1 = Value::Timestamp(timestamp);
        let bytes = encode_page(&[values], &planned, 1, 20, 1).unwrap();
        assert_eq!(
            bytes,
            r#"{"items":[{"displayName":null,"created":"2026-11-16T10:00:00.123000+00:00","count":9223372036854775807}],"page":1,"page_size":20,"total":1,"pages":1}"#.as_bytes()
        );
    }

    #[test]
    fn rejects_column_order_width_null_and_value_mismatch() {
        let rows = [row(Value::Text("ok".into()), Value::Float(1.0))];
        let mut reordered = rows[0].clone();
        reordered.swap(0, 1);
        assert!(encode_page(&[reordered], &plan(), 1, 20, 1).is_none());
        assert!(encode_page(&[rows[0][..3].to_vec()], &plan(), 1, 20, 1).is_none());
        assert!(encode_page(&[row(Value::Null, Value::Float(1.0))], &plan(), 1, 20, 1).is_some());
        assert!(encode_page(
            &[row(Value::Text("x".into()), Value::Int(1))],
            &plan(),
            1,
            20,
            1
        )
        .is_none());
        assert!(encode_page(&rows, &plan(), 0, 20, 1).is_none());
    }

    #[test]
    fn falls_back_for_unrepresentable_projected_and_unprojected_timestamps() {
        let projected_plan = RowPlan::new(
            vec!["created".into()],
            vec![("created".into(), "created".into(), "datetime".into(), false)],
        )
        .unwrap();
        let unprojected_plan = RowPlan::new(
            vec!["id".into(), "created".into()],
            vec![("id".into(), "id".into(), "int".into(), false)],
        )
        .unwrap();
        let year_zero = datetime!(0000-01-01 0:00 UTC);
        let projected = vec![("created".into(), Value::Timestamp(year_zero))];
        assert!(encode_page(&[projected], &projected_plan, 1, 20, 1).is_none());

        let beyond_python = datetime!(+10000-01-01 0:00 UTC);
        let projected_late = vec![("created".into(), Value::Timestamp(beyond_python))];
        assert!(encode_page(&[projected_late], &projected_plan, 1, 20, 1).is_none());
        let unprojected = vec![
            ("id".into(), Value::Int(1)),
            ("created".into(), Value::Timestamp(beyond_python)),
        ];
        assert!(encode_page(&[unprojected], &unprojected_plan, 1, 20, 1).is_none());
    }
}
