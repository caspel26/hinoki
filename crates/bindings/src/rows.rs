//! Rust-only row projection and JSON encoding for eligible generated CRUD pages.

use hinoki_orm::links::{Canon, KeyKind};
use hinoki_orm::Value;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use std::collections::{HashMap, HashSet};
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
    /// A `Decimal` stored as text, written in fixed notation.
    Decimal,
    /// A `date` stored as `YYYY-MM-DD` text.
    Date,
    /// The set of linked keys of a many-to-many relation, read apart from the row.
    Ids,
    /// A `select_related` forward relation encoded from joined alias columns.
    Nested,
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
            "decimal" => Ok(Self::Decimal),
            "date" => Ok(Self::Date),
            "ids" => Ok(Self::Ids),
            "nested" => Ok(Self::Nested),
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
    /// For an ids or nested field, its position among the plan's slots.
    slot: usize,
    name: String,
}

/// One `select_related` forward relation projected from joined SQL alias columns.
#[derive(Clone, Debug)]
struct NestedPlan {
    fk_column_index: usize,
    key_column_index: usize,
    fields: Vec<Field>,
}

/// Metadata only: this plan owns no Python objects and is safe in a Tokio task.
#[derive(Clone, Debug)]
pub(crate) struct RowPlan {
    columns: Vec<String>,
    fields: Vec<Field>,
    nested: Vec<NestedPlan>,
    key_index: Option<usize>,
    key_kind: KeyKind,
}

/// Raw nested field input from Python: `(field_name, fk_column, key_column, sub_fields)`.
pub(crate) type RawNested = (String, String, String, Vec<(String, String, String, bool)>);

/// The linked keys of the rows being written out, one map per ids field.
#[derive(Debug, Default)]
pub(crate) struct RowIds {
    slots: Vec<HashMap<Canon, Vec<Canon>>>,
}

impl RowIds {
    pub(crate) fn new(slots: Vec<HashMap<Canon, Vec<Canon>>>) -> Self {
        Self { slots }
    }
}

pub(crate) enum PageWireResult {
    Encoded(Vec<u8>),
    Fallback(Vec<Vec<(String, Value)>>),
}

pub(crate) enum ProjectedListResult {
    Encoded(Vec<u8>, Vec<Vec<(String, Value)>>),
    Fallback(Vec<Vec<(String, Value)>>),
}

pub(crate) enum ProjectedPageResult {
    Encoded(Vec<u8>, Vec<Vec<(String, Value)>>, u64),
    Fallback(Vec<Vec<(String, Value)>>, i64),
}

/// Owned Rust rows together with their pre-encoded compact UTF-8 JSON bytes.
///
/// Returned by `fetch_projected_list` / `fetch_projected_page` so HTTP JSON
/// responses can emit `json` directly without allocating Python `Model` or
/// `Schema` instances, while Python attribute/sequence access materializes
/// from `rows` without issuing a second database query.
#[pyclass(name = "_ProjectedRows", module = "hinoki.core")]
pub(crate) struct PyProjectedRows {
    pub(crate) json: Vec<u8>,
    rows: Vec<Vec<(String, Value)>>,
    pub(crate) page_info: Option<(u64, u64, u64)>,
}

impl PyProjectedRows {
    pub(crate) fn new(
        json: Vec<u8>,
        rows: Vec<Vec<(String, Value)>>,
        page_info: Option<(u64, u64, u64)>,
    ) -> Self {
        Self {
            json,
            rows,
            page_info,
        }
    }
}

#[pymethods]
impl PyProjectedRows {
    #[getter]
    fn len(&self) -> usize {
        self.rows.len()
    }

    #[getter]
    fn page_info(&self) -> Option<(u64, u64, u64)> {
        self.page_info
    }

    fn json_bytes<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.json)
    }

    fn rows<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        crate::db::rows_to_py(py, &self.rows).map(|obj| obj.into_bound(py))
    }
}

#[pyclass(name = "_RowPlan", module = "hinoki.core")]
pub(crate) struct PyRowPlan {
    pub(crate) inner: Arc<RowPlan>,
}

#[pymethods]
impl PyRowPlan {
    /// `columns` is the complete SQL projection; fields are in Schema order.
    #[new]
    #[pyo3(signature = (columns, fields, nested=Vec::new()))]
    fn new(
        columns: Vec<String>,
        fields: Vec<(String, String, String, bool)>,
        nested: Vec<RawNested>,
    ) -> PyResult<Self> {
        RowPlan::with_nested(columns, fields, nested).map(|inner| Self {
            inner: Arc::new(inner),
        })
    }
}

fn column_position(columns: &[String], name: &str) -> PyResult<usize> {
    columns
        .iter()
        .position(|column| column == name)
        .ok_or_else(|| PyValueError::new_err(format!("unknown row column {name:?}")))
}

fn check_unique_columns(columns: &[String]) -> PyResult<()> {
    let mut seen = HashSet::with_capacity(columns.len());
    for column in columns {
        if !seen.insert(column) {
            return Err(PyValueError::new_err("duplicate RowPlan SQL column"));
        }
    }
    Ok(())
}

fn build_nested_plans(
    columns: &[String],
    raw: Vec<RawNested>,
) -> PyResult<HashMap<String, NestedPlan>> {
    let mut map = HashMap::with_capacity(raw.len());
    for (field_name, fk_column, key_column, sub_fields) in raw {
        let fk_column_index = column_position(columns, &fk_column)?;
        let key_column_index = column_position(columns, &key_column)?;
        let mut wire_names = HashSet::with_capacity(sub_fields.len());
        let mut fields = Vec::with_capacity(sub_fields.len());
        for (col_name, wire_name, kind, nullable) in sub_fields {
            if !wire_names.insert(wire_name.clone()) {
                return Err(PyValueError::new_err("duplicate nested RowPlan wire name"));
            }
            let parsed = Kind::parse(&kind)?;
            if matches!(parsed, Kind::Ids | Kind::Nested) {
                return Err(PyValueError::new_err(
                    "nested RowPlan fields must be scalars",
                ));
            }
            let column_index = column_position(columns, &col_name)?;
            fields.push(Field {
                column_index,
                wire_name,
                kind: parsed,
                nullable,
                slot: 0,
                name: col_name,
            });
        }
        map.insert(
            field_name,
            NestedPlan {
                fk_column_index,
                key_column_index,
                fields,
            },
        );
    }
    Ok(map)
}

impl RowPlan {
    #[cfg(test)]
    pub(crate) fn new(
        columns: Vec<String>,
        fields: Vec<(String, String, String, bool)>,
    ) -> PyResult<Self> {
        Self::build(columns, fields, Vec::new(), None, KeyKind::Int)
    }

    pub(crate) fn with_nested(
        columns: Vec<String>,
        fields: Vec<(String, String, String, bool)>,
        nested: Vec<RawNested>,
    ) -> PyResult<Self> {
        Self::build(columns, fields, nested, None, KeyKind::Int)
    }

    /// A plan whose `ids` fields are filled from [`RowIds`], keyed by the
    /// row's `key_column`.
    pub(crate) fn with_links(
        columns: Vec<String>,
        fields: Vec<(String, String, String, bool)>,
        (key_column, key_kind): (&str, KeyKind),
    ) -> PyResult<Self> {
        Self::build(columns, fields, Vec::new(), Some(key_column), key_kind)
    }

    fn build(
        columns: Vec<String>,
        fields: Vec<(String, String, String, bool)>,
        raw_nested: Vec<RawNested>,
        key_column: Option<&str>,
        key_kind: KeyKind,
    ) -> PyResult<Self> {
        check_unique_columns(&columns)?;
        let key_index = key_column
            .map(|key| column_position(&columns, key))
            .transpose()?;
        let mut nested_map = build_nested_plans(&columns, raw_nested)?;
        let mut planned = Vec::with_capacity(fields.len());
        let mut nested = Vec::new();
        let mut wire_names = HashSet::with_capacity(fields.len());
        let mut ids_slots = 0;
        for (name, wire_name, kind, nullable) in fields {
            if !wire_names.insert(wire_name.clone()) {
                return Err(PyValueError::new_err("duplicate RowPlan wire name"));
            }
            let kind = Kind::parse(&kind)?;
            let (column_index, slot) = resolve_field_slot(
                &columns,
                &name,
                kind,
                key_index,
                &mut ids_slots,
                &mut nested_map,
                &mut nested,
            )?;
            planned.push(Field {
                column_index,
                wire_name,
                kind,
                nullable,
                slot,
                name,
            });
        }
        Ok(Self {
            columns,
            fields: planned,
            nested,
            key_index,
            key_kind,
        })
    }

    /// The names of the ids fields, in slot order.
    pub(crate) fn ids_names(&self) -> Vec<&str> {
        self.fields
            .iter()
            .filter(|field| field.kind == Kind::Ids)
            .map(|field| field.name.as_str())
            .collect()
    }

    /// The key of `row`, as the ids maps are keyed.
    pub(crate) fn key_of(&self, row: &[(String, Value)]) -> Option<Canon> {
        self.key_kind.read(&row.get(self.key_index?)?.1)
    }
}

fn resolve_field_slot(
    columns: &[String],
    name: &str,
    kind: Kind,
    key_index: Option<usize>,
    ids_slots: &mut usize,
    nested_map: &mut HashMap<String, NestedPlan>,
    nested: &mut Vec<NestedPlan>,
) -> PyResult<(usize, usize)> {
    match kind {
        Kind::Ids => {
            if key_index.is_none() {
                return Err(PyValueError::new_err("an ids field needs a row key"));
            }
            let slot = *ids_slots;
            *ids_slots += 1;
            Ok((usize::MAX, slot))
        }
        Kind::Nested => {
            let plan = nested_map
                .remove(name)
                .ok_or_else(|| PyValueError::new_err(format!("missing nested plan {name:?}")))?;
            let slot = nested.len();
            nested.push(plan);
            Ok((usize::MAX, slot))
        }
        _ => Ok((column_position(columns, name)?, 0)),
    }
}

pub(crate) fn encode_list(rows: &[Vec<(String, Value)>], plan: &RowPlan) -> Option<Vec<u8>> {
    let mut output = Vec::with_capacity(rows.len().saturating_mul(64) + 2);
    output.push(b'[');
    for (index, row) in rows.iter().enumerate() {
        if index > 0 {
            output.push(b',');
        }
        write_row(&mut output, row, plan, &RowIds::default())?;
    }
    output.push(b']');
    Some(output)
}

pub(crate) fn encode_page(
    rows: &[Vec<(String, Value)>],
    plan: &RowPlan,
    page: u64,
    page_size: u64,
    total: u64,
) -> Option<Vec<u8>> {
    encode_page_with(rows, plan, &RowIds::default(), (page, page_size, total))
}

pub(crate) fn encode_page_with(
    rows: &[Vec<(String, Value)>],
    plan: &RowPlan,
    ids: &RowIds,
    (page, page_size, total): (u64, u64, u64),
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
        write_row(&mut output, row, plan, ids)?;
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
        write_row(&mut output, row, plan, &RowIds::default())?;
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

pub(crate) fn encode_item_with(
    row: &[(String, Value)],
    plan: &RowPlan,
    ids: &RowIds,
) -> Option<Vec<u8>> {
    let mut output = Vec::new();
    write_row(&mut output, row, plan, ids)?;
    Some(output)
}

fn write_row(
    output: &mut Vec<u8>,
    row: &[(String, Value)],
    plan: &RowPlan,
    ids: &RowIds,
) -> Option<()> {
    validate_columns(row, &plan.columns)?;
    output.push(b'{');
    for (index, field) in plan.fields.iter().enumerate() {
        if index > 0 {
            output.push(b',');
        }
        write_key(output, &field.wire_name)?;
        match field.kind {
            Kind::Ids => write_ids(output, ids.slots.get(field.slot)?.get(&plan.key_of(row)?))?,
            Kind::Nested => {
                write_nested(output, row, plan.nested.get(field.slot)?, field.nullable)?
            }
            _ => write_value(output, &row[field.column_index].1, field)?,
        }
    }
    output.push(b'}');
    Some(())
}

fn write_nested(
    output: &mut Vec<u8>,
    row: &[(String, Value)],
    nested: &NestedPlan,
    nullable: bool,
) -> Option<()> {
    if matches!(row[nested.key_column_index].1, Value::Null) {
        if !nullable || !matches!(row[nested.fk_column_index].1, Value::Null) {
            return None;
        }
        output.extend_from_slice(b"null");
        return Some(());
    }
    output.push(b'{');
    for (index, field) in nested.fields.iter().enumerate() {
        if index > 0 {
            output.push(b',');
        }
        write_key(output, &field.wire_name)?;
        write_value(output, &row[field.column_index].1, field)?;
    }
    output.push(b'}');
    Some(())
}

/// A row without links shows an empty list.
fn write_ids(output: &mut Vec<u8>, keys: Option<&Vec<Canon>>) -> Option<()> {
    output.push(b'[');
    for (index, key) in keys.into_iter().flatten().enumerate() {
        if index > 0 {
            output.push(b',');
        }
        match key {
            Canon::Int(number) => write_number(output, number)?,
            Canon::Text(text) => write_json(output, text)?,
            Canon::Uuid(id) => write_json(output, &id.to_string())?,
        }
    }
    output.push(b']');
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
        (Kind::Decimal, Value::Text(value)) => write_json(output, &decimal_wire(value)?),
        (Kind::Date, Value::Text(value)) => {
            crate::schema::parse_date(value)?;
            write_json(output, value)
        }
        _ => None,
    }
}

/// `format(Decimal(text), "f")` for stored text without an exponent: the
/// text with the integer part's leading zeros removed. Any other spelling,
/// or one longer than the JSON writer allows, returns `None` so Python
/// writes it.
fn decimal_wire(text: &str) -> Option<String> {
    let (sign, unsigned) = match text.strip_prefix('-') {
        Some(rest) => ("-", rest),
        None => ("", text),
    };
    let (whole, fraction) = match unsigned.split_once('.') {
        Some((whole, fraction)) if !fraction.is_empty() => (whole, Some(fraction)),
        Some(_) => return None,
        None => (unsigned, None),
    };
    let digits = |part: &str| !part.is_empty() && part.bytes().all(|byte| byte.is_ascii_digit());
    if !digits(whole) || !fraction.is_none_or(digits) {
        return None;
    }
    let whole = whole.trim_start_matches('0');
    let whole = if whole.is_empty() { "0" } else { whole };
    let length = whole.len() + fraction.map_or(0, str::len);
    if length > crate::schema::DECIMAL_DIGITS_CEILING {
        return None;
    }
    Some(match fraction {
        Some(fraction) => format!("{sign}{whole}.{fraction}"),
        None => format!("{sign}{whole}"),
    })
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
    use super::{decimal_wire, encode_item_with, encode_page, RowIds, RowPlan};
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
            encode_item_with(&row, &plan, &RowIds::default()).unwrap(),
            r#"{"displayName":"Hinoki 🌲","id":9}"#.as_bytes()
        );
    }

    #[test]
    fn writes_the_sorted_ids_of_a_row_and_an_empty_list_for_an_unlinked_one() {
        use hinoki_orm::links::{Canon, KeyKind};
        use std::collections::HashMap;

        let plan = RowPlan::with_links(
            vec!["id".to_owned()],
            vec![
                ("id".to_owned(), "id".to_owned(), "int".to_owned(), false),
                (
                    "tag_ids".to_owned(),
                    "tagIds".to_owned(),
                    "ids".to_owned(),
                    false,
                ),
            ],
            ("id", KeyKind::Int),
        )
        .unwrap();
        let ids = RowIds::new(vec![HashMap::from([(
            Canon::Int(1),
            vec![Canon::Text("a".into()), Canon::Text("b".into())],
        )])]);
        let one = vec![("id".to_owned(), Value::Int(1))];
        let two = vec![("id".to_owned(), Value::Int(2))];
        assert_eq!(
            encode_item_with(&one, &plan, &ids).unwrap(),
            br#"{"id":1,"tagIds":["a","b"]}"#
        );
        assert_eq!(
            encode_item_with(&two, &plan, &ids).unwrap(),
            br#"{"id":2,"tagIds":[]}"#
        );
        assert!(encode_item_with(&one, &plan, &RowIds::default()).is_none());
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
                slot: 0,
                name: "created".into(),
            },
            super::Field {
                column_index: 3,
                wire_name: "count".into(),
                kind: super::Kind::Int,
                nullable: false,
                slot: 0,
                name: "count".into(),
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

    #[test]
    fn decimal_wire_matches_fixed_point_formatting_or_defers() {
        assert_eq!(decimal_wire("007.10").as_deref(), Some("7.10"));
        assert_eq!(decimal_wire("-0.5").as_deref(), Some("-0.5"));
        assert_eq!(decimal_wire("000").as_deref(), Some("0"));
        for text in ["1E-7", "1.", ".5", "abc", "1.2.3"] {
            assert_eq!(decimal_wire(text), None, "{text}");
        }
    }

    #[test]
    fn decimal_and_date_columns_write_valid_text_only() {
        let plan = RowPlan::new(
            vec!["price".into(), "due".into()],
            vec![
                ("price".into(), "price".into(), "decimal".into(), false),
                ("due".into(), "due".into(), "date".into(), true),
            ],
        )
        .unwrap();
        let row = |price: &str, due: &str| {
            vec![
                ("price".to_owned(), Value::Text(price.to_owned())),
                ("due".to_owned(), Value::Text(due.to_owned())),
            ]
        };
        let ids = RowIds::default();
        assert_eq!(
            encode_item_with(&row("012.50", "2026-02-28"), &plan, &ids).unwrap(),
            br#"{"price":"12.50","due":"2026-02-28"}"#
        );
        assert!(encode_item_with(&row("1.50", "2026-02-30"), &plan, &ids).is_none());
        assert!(encode_item_with(&row("1E-7", "2026-02-28"), &plan, &ids).is_none());
    }

    #[test]
    fn encodes_list_and_nested_select_related_rows() {
        let plan = RowPlan::with_nested(
            vec![
                "id".into(),
                "title".into(),
                "author_id".into(),
                "rel_author__0".into(),
                "rel_author__1".into(),
            ],
            vec![
                ("id".into(), "id".into(), "int".into(), false),
                ("title".into(), "title".into(), "str".into(), false),
                ("author".into(), "writer".into(), "nested".into(), true),
            ],
            vec![(
                "author".into(),
                "author_id".into(),
                "rel_author__0".into(),
                vec![
                    ("rel_author__0".into(), "id".into(), "int".into(), false),
                    ("rel_author__1".into(), "name".into(), "str".into(), false),
                ],
            )],
        )
        .unwrap();
        let linked = vec![
            ("id".into(), Value::Int(1)),
            ("title".into(), Value::Text("First".into())),
            ("author_id".into(), Value::Int(10)),
            ("rel_author__0".into(), Value::Int(10)),
            ("rel_author__1".into(), Value::Text("Ada".into())),
        ];
        let unlinked = vec![
            ("id".into(), Value::Int(2)),
            ("title".into(), Value::Text("Draft".into())),
            ("author_id".into(), Value::Null),
            ("rel_author__0".into(), Value::Null),
            ("rel_author__1".into(), Value::Null),
        ];
        let dangling = vec![
            ("id".into(), Value::Int(3)),
            ("title".into(), Value::Text("Orphan".into())),
            ("author_id".into(), Value::Int(99)),
            ("rel_author__0".into(), Value::Null),
            ("rel_author__1".into(), Value::Null),
        ];
        assert_eq!(
            super::encode_list(&[linked, unlinked], &plan).unwrap(),
            br#"[{"id":1,"title":"First","writer":{"id":10,"name":"Ada"}},{"id":2,"title":"Draft","writer":null}]"#
        );
        assert!(super::encode_list(&[dangling], &plan).is_none());
    }
}
