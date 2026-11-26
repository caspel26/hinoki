//! Rust-only row projection and JSON encoding for eligible generated CRUD pages.

use bytes::Bytes;
use hinoki_orm::links::{Canon, KeyKind};
use hinoki_orm::query::{
    compile_select_bound, compile_select_linked, Dialect, Filter, Join, Op, QueryError, Select,
};
use hinoki_orm::{RowSet, Value};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};
use std::borrow::Cow;
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
    /// A 1-level `prefetch_related` relation (M2M or reverse FK) encoded from child rows.
    Prefetch,
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
            "prefetch" => Ok(Self::Prefetch),
            _ => Err(PyValueError::new_err(format!(
                "unsupported row wire kind {value:?}"
            ))),
        }
    }
}

const MAX_INITIAL_CAPACITY: usize = 4 * 1024 * 1024;

#[derive(Clone, Debug)]
struct Field {
    column_index: usize,
    quoted_key: Box<[u8]>,
    kind: Kind,
    nullable: bool,
    /// For an ids, nested or prefetch field, its position among the plan's slots.
    slot: usize,
    name: String,
}

fn encode_quoted_key(wire_name: &str) -> Box<[u8]> {
    let mut buf = Vec::with_capacity(wire_name.len() + 3);
    crate::request::push_json_string(&mut buf, wire_name);
    buf.push(b':');
    buf.into_boxed_slice()
}

fn fixed_field_bytes(field: &Field) -> usize {
    let value_bytes = match field.kind {
        Kind::Uuid => 39,
        Kind::Timestamp => 35,
        Kind::Date => 13,
        _ => 8,
    };
    field.quoted_key.len().saturating_add(value_bytes)
}

fn fixed_fields_bytes(fields: &[Field]) -> usize {
    fields.iter().map(fixed_field_bytes).sum()
}

/// One `select_related` forward relation projected from joined SQL alias columns.
#[derive(Clone, Debug)]
struct NestedPlan {
    fk_column_index: usize,
    key_column_index: usize,
    fields: Vec<Field>,
}

/// Bridge table metadata for an M2M `prefetch_related` relation.
#[derive(Clone, Debug)]
pub(crate) struct BridgeSpec {
    pub(crate) table: String,
    pub(crate) alias: String,
    pub(crate) target_pk: String,
    pub(crate) far_col: String,
    pub(crate) near_col: String,
}

/// One 1-level `prefetch_related` relation (M2M or reverse FK).
#[derive(Clone, Debug)]
pub(crate) struct PrefetchPlan {
    pub(crate) name: String,
    pub(crate) table: String,
    pub(crate) columns: Vec<String>,
    pub(crate) soft_delete: Option<String>,
    pub(crate) order: Vec<(String, bool)>,
    pub(crate) bridge: Option<BridgeSpec>,
    pub(crate) filter_column: String,
    pub(crate) link_column_index: usize,
    pub(crate) child_plan: RowPlan,
    pub(crate) max_items: Option<usize>,
    pub(crate) dialect: Dialect,
    /// Pre-compiled `(sql, optional_limit_param)` template for `owners.len() == 1`.
    pub(crate) single_owner_template: Option<(String, Option<Value>)>,
}

impl PrefetchPlan {
    /// Compile the SQL and bound values for one chunk of owner keys.
    pub(crate) fn compile_chunk(
        &self,
        owners: &[Value],
    ) -> Result<(Cow<'_, str>, Vec<Value>), QueryError> {
        if let ([owner], Some((sql, limit_param))) = (owners, &self.single_owner_template) {
            let mut params = Vec::with_capacity(if limit_param.is_some() { 2 } else { 1 });
            params.push(owner.clone());
            if let Some(lim) = limit_param {
                params.push(lim.clone());
            }
            return Ok((Cow::Borrowed(sql.as_str()), params));
        }
        self.compile_chunk_dynamic(owners)
            .map(|(sql, params)| (Cow::Owned(sql), params))
    }

    fn compile_chunk_dynamic(&self, owners: &[Value]) -> Result<(String, Vec<Value>), QueryError> {
        let mut filters = Vec::with_capacity(2);
        if let Some(col) = &self.soft_delete {
            filters.push(Filter {
                column: col.clone(),
                op: Op::IsNull,
                values: Vec::new(),
            });
        }
        filters.push(Filter {
            column: self.filter_column.clone(),
            op: Op::In,
            values: owners.to_vec(),
        });
        let (joins, limit, linked) = match &self.bridge {
            Some(bridge) => {
                let join = Join {
                    table: bridge.table.clone(),
                    alias: bridge.alias.clone(),
                    local: bridge.target_pk.clone(),
                    remote: bridge.far_col.clone(),
                    left: false,
                    columns: vec![bridge.near_col.clone()],
                    live: None,
                };
                let limit = self
                    .max_items
                    .map(|m| owners.len().saturating_mul(m).saturating_add(1) as u64);
                (vec![join], limit, true)
            }
            None => (Vec::new(), None, false),
        };
        let select = Select {
            table: self.table.clone(),
            columns: self.columns.clone(),
            joins,
            filters,
            subfilters: Vec::new(),
            keyset: None,
            order: self.order.clone(),
            limit,
            offset: None,
            count: false,
        };
        let mut bind_int = Value::Int;
        if linked {
            compile_select_linked(&select, self.dialect, &mut bind_int)
        } else {
            compile_select_bound(&select, self.dialect, &mut bind_int)
        }
    }
}

/// Raw prefetch input from Python.
#[derive(FromPyObject)]
pub(crate) struct RawPrefetch(
    String,
    String,
    Vec<String>,
    Option<String>,
    Vec<(String, bool)>,
    Option<(String, String, String, String, String)>,
    String,
    String,
    Vec<(String, String, String, bool)>,
    Option<usize>,
    String,
);

/// Pre-encoded child JSON items for one prefetch slot.
#[derive(Clone, Debug)]
pub(crate) enum PrefetchSlot {
    /// Fast path when there is at most one owner: avoids `HashMap` allocations.
    Single { owner: Canon, items: Vec<u8> },
    /// General multi-owner path keyed by parent canonical key.
    Multi(HashMap<Canon, Vec<u8>>),
}

impl PrefetchSlot {
    pub(crate) fn empty() -> Self {
        Self::Multi(HashMap::new())
    }

    fn get(&self, key: &Canon) -> Option<&[u8]> {
        match self {
            Self::Single { owner, items } => (owner == key).then_some(items.as_slice()),
            Self::Multi(map) => map.get(key).map(Vec::as_slice),
        }
    }

    fn estimated_bytes(&self) -> usize {
        match self {
            Self::Single { items, .. } => items.len(),
            Self::Multi(map) => map.values().map(Vec::len).sum(),
        }
    }
}

/// Pre-encoded child JSON arrays per prefetch slot, keyed by parent canonical key.
pub(crate) type PrefetchedJson = Vec<PrefetchSlot>;

/// Raw child rows per prefetch slot, retained for lazy Python materialization.
pub(crate) type PrefetchedRows = Vec<(String, RowSet)>;

/// Metadata only: this plan owns no Python objects and is safe in a Tokio task.
#[derive(Clone, Debug)]
pub(crate) struct RowPlan {
    columns: Vec<String>,
    fields: Vec<Field>,
    nested: Vec<NestedPlan>,
    prefetches: Vec<PrefetchPlan>,
    key_index: Option<usize>,
    key_kind: KeyKind,
    fixed_row_bytes: usize,
    text_columns: Box<[usize]>,
    bytes_columns: Box<[usize]>,
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
    Encoded(Vec<u8>, RowSet, PrefetchedRows),
    Fallback(RowSet),
}

pub(crate) enum ProjectedPageResult {
    Encoded(Vec<u8>, RowSet, PrefetchedRows, u64),
    Fallback(RowSet, i64),
}

pub(crate) enum ProjectedOneResult {
    Encoded(Vec<u8>, RowSet, PrefetchedRows),
    Empty,
    Multiple,
    Fallback(RowSet),
}

/// Owned Rust rows together with their pre-encoded compact UTF-8 JSON bytes.
///
/// Returned by `fetch_projected_list` / `fetch_projected_page` / `fetch_projected_one`
/// so HTTP JSON responses can emit `json` directly without allocating Python
/// `Model` or `Schema` instances, while Python attribute/sequence access
/// materializes from `rows` (and `prefetched`) without issuing a second query.
#[pyclass(name = "_ProjectedRows", module = "hinoki.core")]
pub(crate) struct PyProjectedRows {
    pub(crate) json: Bytes,
    rows: RowSet,
    prefetched: PrefetchedRows,
    pub(crate) page_info: Option<(u64, u64, u64)>,
}

impl PyProjectedRows {
    pub(crate) fn new(
        json: impl Into<Bytes>,
        rows: RowSet,
        prefetched: PrefetchedRows,
        page_info: Option<(u64, u64, u64)>,
    ) -> Self {
        Self {
            json: json.into(),
            rows,
            prefetched,
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
        crate::db::row_set_to_py(py, &self.rows).map(|obj| obj.into_bound(py))
    }

    fn prefetched_rows<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let out = PyDict::new(py);
        for (name, rows) in &self.prefetched {
            let py_rows = crate::db::row_set_to_py(py, rows)?;
            out.set_item(name, py_rows)?;
        }
        Ok(out)
    }
}

#[pyclass(name = "_RowPlan", module = "hinoki.core")]
pub(crate) struct PyRowPlan {
    pub(crate) inner: Arc<RowPlan>,
}

fn parse_key_kind(kind: &str) -> PyResult<KeyKind> {
    match kind {
        "int" => Ok(KeyKind::Int),
        "text" | "str" => Ok(KeyKind::Text {
            max_length: usize::MAX,
        }),
        "uuid" => Ok(KeyKind::Uuid),
        _ => Err(PyValueError::new_err(format!(
            "unsupported key kind {kind:?}"
        ))),
    }
}

#[pymethods]
impl PyRowPlan {
    /// `columns` is the complete SQL projection; fields are in Schema order.
    #[new]
    #[pyo3(signature = (columns, fields, nested=Vec::new(), prefetches=Vec::new(), key=None))]
    fn new(
        columns: Vec<String>,
        fields: Vec<(String, String, String, bool)>,
        nested: Vec<RawNested>,
        prefetches: Vec<RawPrefetch>,
        key: Option<(String, String)>,
    ) -> PyResult<Self> {
        let (key_col, key_kind) = match key {
            Some((col, kind)) => (Some(col), parse_key_kind(&kind)?),
            None => (None, KeyKind::Int),
        };
        RowPlan::with_relations(
            columns,
            fields,
            nested,
            prefetches,
            key_col.as_deref(),
            key_kind,
        )
        .map(|inner| Self {
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
                quoted_key: encode_quoted_key(&wire_name),
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

fn build_prefetch_plans(raw: Vec<RawPrefetch>) -> PyResult<HashMap<String, PrefetchPlan>> {
    let mut map = HashMap::with_capacity(raw.len());
    for entry in raw {
        let RawPrefetch(
            name,
            table,
            columns,
            soft_delete,
            order,
            bridge,
            filter_column,
            link_column,
            fields,
            max_items,
            dialect_str,
        ) = entry;
        let dialect = crate::query::parse_dialect(&dialect_str)?;
        let bridge = bridge.map(|(t, alias, target_pk, far_col, near_col)| BridgeSpec {
            table: t,
            alias,
            target_pk,
            far_col,
            near_col,
        });
        let mut sql_columns = columns.clone();
        if bridge.is_some() {
            sql_columns.push(link_column.clone());
        }
        let link_column_index = column_position(&sql_columns, &link_column)?;
        let child_plan = RowPlan::build(
            sql_columns,
            fields,
            Vec::new(),
            Vec::new(),
            None,
            KeyKind::Int,
        )?;
        let mut plan = PrefetchPlan {
            name: name.clone(),
            table,
            columns,
            soft_delete,
            order,
            bridge,
            filter_column,
            link_column_index,
            child_plan,
            max_items,
            dialect,
            single_owner_template: None,
        };
        if let Ok((sql, mut dummy_params)) = plan.compile_chunk_dynamic(&[Value::Null]) {
            let limit_param = if dummy_params.len() > 1 {
                Some(dummy_params.swap_remove(1))
            } else {
                None
            };
            plan.single_owner_template = Some((sql, limit_param));
        }
        map.insert(name, plan);
    }
    Ok(map)
}

impl RowPlan {
    #[cfg(test)]
    pub(crate) fn new(
        columns: Vec<String>,
        fields: Vec<(String, String, String, bool)>,
    ) -> PyResult<Self> {
        Self::build(columns, fields, Vec::new(), Vec::new(), None, KeyKind::Int)
    }

    #[cfg(test)]
    pub(crate) fn with_nested(
        columns: Vec<String>,
        fields: Vec<(String, String, String, bool)>,
        nested: Vec<RawNested>,
    ) -> PyResult<Self> {
        Self::build(columns, fields, nested, Vec::new(), None, KeyKind::Int)
    }

    pub(crate) fn with_relations(
        columns: Vec<String>,
        fields: Vec<(String, String, String, bool)>,
        nested: Vec<RawNested>,
        prefetches: Vec<RawPrefetch>,
        key_column: Option<&str>,
        key_kind: KeyKind,
    ) -> PyResult<Self> {
        Self::build(columns, fields, nested, prefetches, key_column, key_kind)
    }

    /// A plan whose `ids` fields are filled from [`RowIds`], keyed by the
    /// row's `key_column`.
    pub(crate) fn with_links(
        columns: Vec<String>,
        fields: Vec<(String, String, String, bool)>,
        (key_column, key_kind): (&str, KeyKind),
    ) -> PyResult<Self> {
        Self::build(
            columns,
            fields,
            Vec::new(),
            Vec::new(),
            Some(key_column),
            key_kind,
        )
    }

    fn build(
        columns: Vec<String>,
        fields: Vec<(String, String, String, bool)>,
        raw_nested: Vec<RawNested>,
        raw_prefetches: Vec<RawPrefetch>,
        key_column: Option<&str>,
        key_kind: KeyKind,
    ) -> PyResult<Self> {
        check_unique_columns(&columns)?;
        let key_index = key_column
            .map(|key| column_position(&columns, key))
            .transpose()?;
        let mut nested_map = build_nested_plans(&columns, raw_nested)?;
        let mut prefetch_map = build_prefetch_plans(raw_prefetches)?;
        let mut planned = Vec::with_capacity(fields.len());
        let mut nested = Vec::new();
        let mut prefetches = Vec::new();
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
                (&mut nested_map, &mut nested),
                (&mut prefetch_map, &mut prefetches),
            )?;
            planned.push(Field {
                column_index,
                quoted_key: encode_quoted_key(&wire_name),
                kind,
                nullable,
                slot,
                name,
            });
        }
        let nested_bytes: usize = nested
            .iter()
            .map(|item| fixed_fields_bytes(&item.fields) + 2)
            .sum();
        let fixed_row_bytes = fixed_fields_bytes(&planned)
            .saturating_add(nested_bytes)
            .saturating_add(2);
        let (text_columns, bytes_columns) = collect_dynamic_columns(&planned, &nested);
        Ok(Self {
            columns,
            fields: planned,
            nested,
            prefetches,
            key_index,
            key_kind,
            fixed_row_bytes,
            text_columns,
            bytes_columns,
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

    pub(crate) fn prefetches(&self) -> &[PrefetchPlan] {
        &self.prefetches
    }

    pub(crate) fn key_kind(&self) -> KeyKind {
        self.key_kind
    }

    /// The key of `row`, as the ids/prefetch maps are keyed.
    pub(crate) fn key_of(&self, row: &[(String, Value)]) -> Option<Canon> {
        self.key_kind.read(&row.get(self.key_index?)?.1)
    }

    /// The key of a positional `row`, as the ids/prefetch maps are keyed.
    pub(crate) fn key_of_values(&self, row: &[Value]) -> Option<Canon> {
        self.key_kind.read(row.get(self.key_index?)?)
    }

    /// Unique parent keys in row order from a `RowSet`, plus the single canonical
    /// key when there is exactly one unique owner.
    pub(crate) fn owner_values_set(&self, set: &RowSet) -> Option<(Vec<Value>, Option<Canon>)> {
        let key_index = self.key_index?;
        if let [only] = set.rows.as_slice() {
            let val = only.get(key_index)?;
            let canon = self.key_kind.read(val)?;
            return Some((vec![val.clone()], Some(canon)));
        }
        let mut seen = HashSet::with_capacity(set.rows.len());
        let mut out = Vec::with_capacity(set.rows.len());
        let mut first_canon = None;
        for row in &set.rows {
            let val = row.get(key_index)?;
            let canon = self.key_kind.read(val)?;
            if seen.insert(canon.clone()) {
                if first_canon.is_none() {
                    first_canon = Some(canon);
                }
                out.push(val.clone());
            }
        }
        let single = if out.len() == 1 { first_canon } else { None };
        Some((out, single))
    }
}

fn resolve_field_slot(
    columns: &[String],
    name: &str,
    kind: Kind,
    key_index: Option<usize>,
    ids_slots: &mut usize,
    nested_state: (&mut HashMap<String, NestedPlan>, &mut Vec<NestedPlan>),
    prefetch_state: (&mut HashMap<String, PrefetchPlan>, &mut Vec<PrefetchPlan>),
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
            let plan = nested_state
                .0
                .remove(name)
                .ok_or_else(|| PyValueError::new_err(format!("missing nested plan {name:?}")))?;
            let slot = nested_state.1.len();
            nested_state.1.push(plan);
            Ok((usize::MAX, slot))
        }
        Kind::Prefetch => {
            if key_index.is_none() {
                return Err(PyValueError::new_err("a prefetch field needs a row key"));
            }
            let plan = prefetch_state
                .0
                .remove(name)
                .ok_or_else(|| PyValueError::new_err(format!("missing prefetch plan {name:?}")))?;
            let slot = prefetch_state.1.len();
            prefetch_state.1.push(plan);
            Ok((usize::MAX, slot))
        }
        _ => Ok((column_position(columns, name)?, 0)),
    }
}

/// Group child rows by parent key and pre-encode each child as compact JSON.
/// Returns `None` on column/value mismatch or M2M `max_items` overflow.
pub(crate) fn group_prefetch_rows(
    prefetch: &PrefetchPlan,
    key_kind: KeyKind,
    set: &RowSet,
) -> Option<PrefetchSlot> {
    if !set.rows.is_empty() {
        validate_column_names(&set.columns, &prefetch.child_plan.columns)?;
    }
    let mut grouped: HashMap<Canon, Vec<u8>> = HashMap::new();
    let mut counts: HashMap<Canon, usize> = HashMap::new();
    for row in &set.rows {
        let owner = key_kind.read(row.get(prefetch.link_column_index)?)?;
        if let Some(limit) = prefetch.max_items {
            let count = counts.entry(owner.clone()).or_insert(0);
            *count += 1;
            if *count > limit {
                return None;
            }
        }
        let buf = grouped
            .entry(owner)
            .or_insert_with(|| Vec::with_capacity(prefetch.child_plan.fixed_row_bytes));
        if !buf.is_empty() {
            buf.push(b',');
        }
        write_row_values(buf, row, &prefetch.child_plan, &RowIds::default(), &[])?;
    }
    Some(PrefetchSlot::Multi(grouped))
}

fn push_dynamic_columns(text_out: &mut Vec<usize>, bytes_out: &mut Vec<usize>, fields: &[Field]) {
    for field in fields {
        let target = match field.kind {
            Kind::Text | Kind::Decimal => &mut *text_out,
            Kind::Bytes => &mut *bytes_out,
            _ => continue,
        };
        if !target.contains(&field.column_index) {
            target.push(field.column_index);
        }
    }
}

fn collect_dynamic_columns(
    fields: &[Field],
    nested: &[NestedPlan],
) -> (Box<[usize]>, Box<[usize]>) {
    let mut text_cols = Vec::with_capacity(fields.len());
    let mut bytes_cols = Vec::new();
    push_dynamic_columns(&mut text_cols, &mut bytes_cols, fields);
    for item in nested {
        push_dynamic_columns(&mut text_cols, &mut bytes_cols, &item.fields);
    }
    (text_cols.into_boxed_slice(), bytes_cols.into_boxed_slice())
}

fn value_extra_bytes(value: &Value, is_bytes: bool) -> usize {
    match (is_bytes, value) {
        (false, Value::Text(text)) => text.len().saturating_add(2),
        (true, Value::Bytes(raw)) => raw.len().div_ceil(3).saturating_mul(4).saturating_add(2),
        _ => 0,
    }
}

fn row_values_extra(row: &[Value], plan: &RowPlan) -> usize {
    let text: usize = plan
        .text_columns
        .iter()
        .filter_map(|&col| row.get(col))
        .map(|val| value_extra_bytes(val, false))
        .sum();
    let bin: usize = plan
        .bytes_columns
        .iter()
        .filter_map(|&col| row.get(col))
        .map(|val| value_extra_bytes(val, true))
        .sum();
    text.saturating_add(bin)
}

fn row_pairs_extra(row: &[(String, Value)], plan: &RowPlan) -> usize {
    let text: usize = plan
        .text_columns
        .iter()
        .filter_map(|&col| row.get(col))
        .map(|(_, val)| value_extra_bytes(val, false))
        .sum();
    let bin: usize = plan
        .bytes_columns
        .iter()
        .filter_map(|&col| row.get(col))
        .map(|(_, val)| value_extra_bytes(val, true))
        .sum();
    text.saturating_add(bin)
}

fn estimate_set_capacity(
    set: &RowSet,
    plan: &RowPlan,
    prefetched: &[PrefetchSlot],
    extra: usize,
) -> usize {
    let mut bytes = set
        .rows
        .len()
        .saturating_mul(plan.fixed_row_bytes)
        .saturating_add(extra);
    if !plan.text_columns.is_empty() || !plan.bytes_columns.is_empty() {
        for row in &set.rows {
            bytes = bytes.saturating_add(row_values_extra(row, plan));
        }
    }
    for slot in prefetched {
        bytes = bytes.saturating_add(slot.estimated_bytes());
    }
    bytes.min(MAX_INITIAL_CAPACITY)
}

fn estimate_single_row_set_capacity(
    row: &[Value],
    plan: &RowPlan,
    prefetched: &[PrefetchSlot],
    overhead: usize,
) -> usize {
    let prefetched_bytes = prefetched
        .iter()
        .map(PrefetchSlot::estimated_bytes)
        .fold(0usize, usize::saturating_add);
    plan.fixed_row_bytes
        .saturating_add(row_values_extra(row, plan))
        .saturating_add(prefetched_bytes)
        .saturating_add(overhead)
        .min(MAX_INITIAL_CAPACITY)
}

fn estimate_rows_capacity(
    rows: &[Vec<(String, Value)>],
    plan: &RowPlan,
    prefetched: &[PrefetchSlot],
    extra: usize,
) -> usize {
    let mut bytes = rows
        .len()
        .saturating_mul(plan.fixed_row_bytes)
        .saturating_add(extra);
    if !plan.text_columns.is_empty() || !plan.bytes_columns.is_empty() {
        for row in rows {
            bytes = bytes.saturating_add(row_pairs_extra(row, plan));
        }
    }
    for slot in prefetched {
        bytes = bytes.saturating_add(slot.estimated_bytes());
    }
    bytes.min(MAX_INITIAL_CAPACITY)
}

/// Fast-path prefetch grouping when there is a single parent owner: writes
/// directly into one pre-sized `Vec<u8>` without allocating `HashMap`s.
pub(crate) fn group_prefetch_rows_single(
    prefetch: &PrefetchPlan,
    key_kind: KeyKind,
    owner: Canon,
    set: &RowSet,
) -> Option<PrefetchSlot> {
    if !set.rows.is_empty() {
        validate_column_names(&set.columns, &prefetch.child_plan.columns)?;
    }
    let mut buf = Vec::with_capacity(estimate_set_capacity(set, &prefetch.child_plan, &[], 0));
    let mut count = 0usize;
    for row in &set.rows {
        let row_owner = key_kind.read(row.get(prefetch.link_column_index)?)?;
        if row_owner != owner {
            return group_prefetch_rows(prefetch, key_kind, set);
        }
        if let Some(limit) = prefetch.max_items {
            count += 1;
            if count > limit {
                return None;
            }
        }
        if !buf.is_empty() {
            buf.push(b',');
        }
        write_row_values(&mut buf, row, &prefetch.child_plan, &RowIds::default(), &[])?;
    }
    Some(PrefetchSlot::Single { owner, items: buf })
}

#[cfg(test)]
pub(crate) fn encode_list(rows: &[Vec<(String, Value)>], plan: &RowPlan) -> Option<Vec<u8>> {
    let mut output = Vec::with_capacity(rows.len().saturating_mul(64) + 2);
    output.push(b'[');
    for (index, row) in rows.iter().enumerate() {
        if index > 0 {
            output.push(b',');
        }
        write_row(&mut output, row, plan, &RowIds::default(), &[])?;
    }
    output.push(b']');
    Some(output)
}

pub(crate) fn encode_list_set_with(
    set: &RowSet,
    plan: &RowPlan,
    prefetched: &[PrefetchSlot],
) -> Option<Vec<u8>> {
    if !set.rows.is_empty() {
        validate_column_names(&set.columns, &plan.columns)?;
    }
    let mut output = Vec::with_capacity(estimate_set_capacity(set, plan, prefetched, 2));
    output.push(b'[');
    for (index, row) in set.rows.iter().enumerate() {
        if index > 0 {
            output.push(b',');
        }
        write_row_values(&mut output, row, plan, &RowIds::default(), prefetched)?;
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
    encode_page_full(
        rows,
        plan,
        &RowIds::default(),
        &[],
        (page, page_size, total),
    )
}

pub(crate) fn encode_page_with(
    rows: &[Vec<(String, Value)>],
    plan: &RowPlan,
    ids: &RowIds,
    page_info: (u64, u64, u64),
) -> Option<Vec<u8>> {
    encode_page_full(rows, plan, ids, &[], page_info)
}

pub(crate) fn encode_page_set_with_prefetches(
    set: &RowSet,
    plan: &RowPlan,
    prefetched: &[PrefetchSlot],
    (page, page_size, total): (u64, u64, u64),
) -> Option<Vec<u8>> {
    if page == 0 || page_size == 0 {
        return None;
    }
    if !set.rows.is_empty() {
        validate_column_names(&set.columns, &plan.columns)?;
    }
    let mut output = Vec::with_capacity(estimate_set_capacity(set, plan, prefetched, 96));
    output.extend_from_slice(b"{\"items\":[");
    for (index, row) in set.rows.iter().enumerate() {
        if index > 0 {
            output.push(b',');
        }
        write_row_values(&mut output, row, plan, &RowIds::default(), prefetched)?;
    }
    write_page_trailer(&mut output, page, page_size, total)?;
    Some(output)
}

fn write_page_trailer(output: &mut Vec<u8>, page: u64, page_size: u64, total: u64) -> Option<()> {
    if page == 0 || page_size == 0 {
        return None;
    }
    output.extend_from_slice(b"],\"page\":");
    write_u64(output, page);
    output.extend_from_slice(b",\"page_size\":");
    write_u64(output, page_size);
    output.extend_from_slice(b",\"total\":");
    write_u64(output, total);
    output.extend_from_slice(b",\"pages\":");
    write_u64(output, total.div_ceil(page_size));
    output.push(b'}');
    Some(())
}

fn encode_page_full(
    rows: &[Vec<(String, Value)>],
    plan: &RowPlan,
    ids: &RowIds,
    prefetched: &[PrefetchSlot],
    (page, page_size, total): (u64, u64, u64),
) -> Option<Vec<u8>> {
    if page == 0 || page_size == 0 {
        return None;
    }
    let mut output = Vec::with_capacity(estimate_rows_capacity(rows, plan, prefetched, 96));
    output.extend_from_slice(b"{\"items\":[");
    for (index, row) in rows.iter().enumerate() {
        if index > 0 {
            output.push(b',');
        }
        write_row(&mut output, row, plan, ids, prefetched)?;
    }
    write_page_trailer(&mut output, page, page_size, total)?;
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
    let link_bytes = links.next.as_deref().map_or(4, |s| s.len() + 2)
        + links.previous.as_deref().map_or(4, |s| s.len() + 2);
    let mut output = Vec::with_capacity(estimate_rows_capacity(
        rows,
        plan,
        &[],
        64usize.saturating_add(link_bytes),
    ));
    output.extend_from_slice(b"{\"items\":[");
    for (index, row) in rows.iter().enumerate() {
        if index > 0 {
            output.push(b',');
        }
        write_row(&mut output, row, plan, &RowIds::default(), &[])?;
    }
    output.extend_from_slice(b"],\"next\":");
    write_link(&mut output, links.next.as_deref());
    output.extend_from_slice(b",\"previous\":");
    write_link(&mut output, links.previous.as_deref());
    output.extend_from_slice(b",\"page_size\":");
    write_u64(&mut output, page_size);
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
    let cap = plan
        .fixed_row_bytes
        .saturating_add(row_pairs_extra(row, plan))
        .saturating_add(32)
        .min(MAX_INITIAL_CAPACITY);
    let mut output = Vec::with_capacity(cap);
    write_row(&mut output, row, plan, ids, &[])?;
    Some(output)
}

pub(crate) fn encode_item_set_with_prefetches(
    set: &RowSet,
    plan: &RowPlan,
    prefetched: &[PrefetchSlot],
) -> Option<Vec<u8>> {
    let row = set.rows.first()?;
    validate_column_names(&set.columns, &plan.columns)?;
    let mut output =
        Vec::with_capacity(estimate_single_row_set_capacity(row, plan, prefetched, 32));
    write_row_values(&mut output, row, plan, &RowIds::default(), prefetched)?;
    Some(output)
}

fn write_row(
    output: &mut Vec<u8>,
    row: &[(String, Value)],
    plan: &RowPlan,
    ids: &RowIds,
    prefetched: &[PrefetchSlot],
) -> Option<()> {
    validate_columns(row, &plan.columns)?;
    output.push(b'{');
    for (index, field) in plan.fields.iter().enumerate() {
        if index > 0 {
            output.push(b',');
        }
        output.extend_from_slice(&field.quoted_key);
        match field.kind {
            Kind::Ids => write_ids(output, ids.slots.get(field.slot)?.get(&plan.key_of(row)?))?,
            Kind::Nested => {
                write_nested(output, row, plan.nested.get(field.slot)?, field.nullable)?
            }
            Kind::Prefetch => {
                let slot = prefetched.get(field.slot)?;
                write_prefetch(output, slot.get(&plan.key_of(row)?));
            }
            _ => write_value(output, &row[field.column_index].1, field)?,
        }
    }
    output.push(b'}');
    Some(())
}

fn write_row_values(
    output: &mut Vec<u8>,
    row: &[Value],
    plan: &RowPlan,
    ids: &RowIds,
    prefetched: &[PrefetchSlot],
) -> Option<()> {
    validate_row_values(row, plan.columns.len())?;
    output.push(b'{');
    for (index, field) in plan.fields.iter().enumerate() {
        if index > 0 {
            output.push(b',');
        }
        output.extend_from_slice(&field.quoted_key);
        match field.kind {
            Kind::Ids => {
                let key = plan.key_of_values(row)?;
                write_ids(output, ids.slots.get(field.slot)?.get(&key))?;
            }
            Kind::Nested => {
                write_nested_values(output, row, plan.nested.get(field.slot)?, field.nullable)?
            }
            Kind::Prefetch => {
                let slot = prefetched.get(field.slot)?;
                write_prefetch(output, slot.get(&plan.key_of_values(row)?));
            }
            _ => write_value(output, &row[field.column_index], field)?,
        }
    }
    output.push(b'}');
    Some(())
}

fn write_prefetch(output: &mut Vec<u8>, items: Option<&[u8]>) {
    output.push(b'[');
    if let Some(bytes) = items {
        output.extend_from_slice(bytes);
    }
    output.push(b']');
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
        output.extend_from_slice(&field.quoted_key);
        write_value(output, &row[field.column_index].1, field)?;
    }
    output.push(b'}');
    Some(())
}

fn write_nested_values(
    output: &mut Vec<u8>,
    row: &[Value],
    nested: &NestedPlan,
    nullable: bool,
) -> Option<()> {
    if matches!(row[nested.key_column_index], Value::Null) {
        if !nullable || !matches!(row[nested.fk_column_index], Value::Null) {
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
        output.extend_from_slice(&field.quoted_key);
        write_value(output, &row[field.column_index], field)?;
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
            Canon::Int(number) => write_i64(output, *number),
            Canon::Text(text) => write_json(output, text)?,
            Canon::Uuid(id) => write_uuid(output, id),
        }
    }
    output.push(b']');
    Some(())
}

fn validate_column_names(actual: &[String], expected: &[String]) -> Option<()> {
    (actual == expected).then_some(())
}

fn validate_row_values(row: &[Value], expected_len: usize) -> Option<()> {
    if row.len() != expected_len {
        return None;
    }
    if row.iter().any(|value| {
        matches!(value, Value::Timestamp(timestamp) if !python_datetime_representable(timestamp))
    }) {
        return None;
    }
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
        (Kind::Int, Value::Int(value)) => {
            write_i64(output, *value);
            Some(())
        }
        (Kind::Float, Value::Float(value)) => write_float(output, *value),
        (Kind::Bool, Value::Bool(value)) => {
            output.extend_from_slice(if *value { b"true" } else { b"false" });
            Some(())
        }
        (Kind::Uuid, Value::Uuid(value)) => {
            write_uuid(output, value);
            Some(())
        }
        (Kind::Timestamp, Value::Timestamp(value)) => {
            if !python_datetime_representable(value) {
                return None;
            }
            output.push(b'"');
            hinoki_orm::write_timestamp_iso(output, value);
            output.push(b'"');
            Some(())
        }
        (Kind::Bytes, Value::Bytes(value)) => write_bytes_base64(output, value),
        (Kind::Decimal, Value::Text(value)) => write_decimal(output, value),
        (Kind::Date, Value::Text(value)) => {
            crate::schema::parse_date(value)?;
            output.push(b'"');
            output.extend_from_slice(value.as_bytes());
            output.push(b'"');
            Some(())
        }
        _ => None,
    }
}

fn write_uuid(output: &mut Vec<u8>, value: &uuid::Uuid) {
    let mut buf = [0u8; uuid::fmt::Hyphenated::LENGTH];
    let text = value.hyphenated().encode_lower(&mut buf);
    output.push(b'"');
    output.extend_from_slice(text.as_bytes());
    output.push(b'"');
}

fn write_bytes_base64(output: &mut Vec<u8>, value: &[u8]) -> Option<()> {
    use base64::{engine::general_purpose::STANDARD, Engine};
    let encoded_len = base64::encoded_len(value.len(), true)?;
    output.reserve(encoded_len.saturating_add(2));
    output.push(b'"');
    let start = output.len();
    let end = start.checked_add(encoded_len)?;
    output.resize(end, 0);
    let written = STANDARD.encode_slice(value, &mut output[start..]).ok()?;
    output.truncate(start.checked_add(written)?);
    output.push(b'"');
    Some(())
}

/// `format(Decimal(text), "f")` components for stored text without an exponent:
/// `(sign, whole, fraction)` with leading zeros removed from `whole`.
fn decimal_parts(text: &str) -> Option<(&str, &str, Option<&str>)> {
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
    Some((sign, whole, fraction))
}

fn write_decimal(output: &mut Vec<u8>, text: &str) -> Option<()> {
    let (sign, whole, fraction) = decimal_parts(text)?;
    output.push(b'"');
    output.extend_from_slice(sign.as_bytes());
    output.extend_from_slice(whole.as_bytes());
    if let Some(fraction) = fraction {
        output.push(b'.');
        output.extend_from_slice(fraction.as_bytes());
    }
    output.push(b'"');
    Some(())
}

#[cfg(test)]
fn decimal_wire(text: &str) -> Option<String> {
    let (sign, whole, fraction) = decimal_parts(text)?;
    Some(match fraction {
        Some(fraction) => format!("{sign}{whole}.{fraction}"),
        None => format!("{sign}{whole}"),
    })
}

fn write_json(output: &mut Vec<u8>, value: &str) -> Option<()> {
    crate::request::push_json_string(output, value);
    Some(())
}

fn write_i64(output: &mut Vec<u8>, value: i64) {
    crate::request::push_i64(output, value);
}

fn write_u64(output: &mut Vec<u8>, value: u64) {
    crate::request::push_u64(output, value);
}

fn write_float(output: &mut Vec<u8>, value: f64) -> Option<()> {
    if !value.is_finite() {
        return None;
    }
    serde_json::to_writer(output, &value).ok()
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
                quoted_key: super::encode_quoted_key("created"),
                kind: super::Kind::Timestamp,
                nullable: false,
                slot: 0,
                name: "created".into(),
            },
            super::Field {
                column_index: 3,
                quoted_key: super::encode_quoted_key("count"),
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

    #[test]
    fn encodes_prefetched_children_and_enforces_max_items() {
        use hinoki_orm::links::{Canon, KeyKind};
        use hinoki_orm::RowSet;
        use std::sync::Arc;

        let plan = RowPlan::with_relations(
            vec!["id".into(), "title".into()],
            vec![
                ("id".into(), "id".into(), "int".into(), false),
                ("title".into(), "title".into(), "str".into(), false),
                ("tags".into(), "tags".into(), "prefetch".into(), false),
            ],
            vec![],
            vec![super::RawPrefetch(
                "tags".into(),
                "tags".into(),
                vec!["id".into(), "label".into()],
                None,
                vec![("id".into(), false)],
                Some((
                    "article_tags".into(),
                    "hinoki_link".into(),
                    "id".into(),
                    "tag_id".into(),
                    "article_id".into(),
                )),
                "hinoki_link.article_id".into(),
                "hinoki_link__0".into(),
                vec![
                    ("id".into(), "id".into(), "int".into(), false),
                    ("label".into(), "label".into(), "str".into(), false),
                ],
                Some(2),
                "sqlite".into(),
            )],
            Some("id"),
            KeyKind::Int,
        )
        .unwrap();

        let parent_cols: Arc<[String]> = Arc::from(["id".to_owned(), "title".to_owned()]);
        let child_cols: Arc<[String]> = Arc::from([
            "id".to_owned(),
            "label".to_owned(),
            "hinoki_link__0".to_owned(),
        ]);

        let parent_list = RowSet {
            columns: parent_cols.clone(),
            rows: vec![
                vec![Value::Int(1), Value::Text("First".into())],
                vec![Value::Int(2), Value::Text("Empty".into())],
            ],
        };
        let parent_one = RowSet {
            columns: parent_cols,
            rows: vec![vec![Value::Int(1), Value::Text("First".into())]],
        };
        let child_set = RowSet {
            columns: child_cols.clone(),
            rows: vec![
                vec![Value::Int(10), Value::Text("rust".into()), Value::Int(1)],
                vec![Value::Int(11), Value::Text("perf".into()), Value::Int(1)],
            ],
        };

        let grouped_multi =
            super::group_prefetch_rows(&plan.prefetches[0], KeyKind::Int, &child_set).unwrap();
        let grouped_single = super::group_prefetch_rows_single(
            &plan.prefetches[0],
            KeyKind::Int,
            Canon::Int(1),
            &child_set,
        )
        .unwrap();

        let encoded_list =
            super::encode_list_set_with(&parent_list, &plan, std::slice::from_ref(&grouped_multi))
                .unwrap();
        assert_eq!(
            encoded_list,
            br#"[{"id":1,"title":"First","tags":[{"id":10,"label":"rust"},{"id":11,"label":"perf"}]},{"id":2,"title":"Empty","tags":[]}]"#
        );

        let expected_item =
            br#"{"id":1,"title":"First","tags":[{"id":10,"label":"rust"},{"id":11,"label":"perf"}]}"#;
        assert_eq!(
            super::encode_item_set_with_prefetches(&parent_one, &plan, &[grouped_multi]).unwrap(),
            expected_item
        );
        assert_eq!(
            super::encode_item_set_with_prefetches(&parent_one, &plan, &[grouped_single]).unwrap(),
            expected_item
        );

        let too_many = RowSet {
            columns: child_cols,
            rows: vec![
                vec![Value::Int(10), Value::Text("a".into()), Value::Int(1)],
                vec![Value::Int(11), Value::Text("b".into()), Value::Int(1)],
                vec![Value::Int(12), Value::Text("c".into()), Value::Int(1)],
            ],
        };
        assert!(super::group_prefetch_rows(&plan.prefetches[0], KeyKind::Int, &too_many).is_none());
        assert!(super::group_prefetch_rows_single(
            &plan.prefetches[0],
            KeyKind::Int,
            Canon::Int(1),
            &too_many
        )
        .is_none());
    }

    #[test]
    fn prefetch_single_owner_template_matches_dynamic_compilation() {
        use hinoki_orm::links::KeyKind;
        use std::borrow::Cow;

        for dialect in ["postgres", "sqlite", "mysql"] {
            let plan = RowPlan::with_relations(
                vec!["id".into()],
                vec![
                    ("id".into(), "id".into(), "int".into(), false),
                    ("tags".into(), "tags".into(), "prefetch".into(), false),
                    (
                        "comments".into(),
                        "comments".into(),
                        "prefetch".into(),
                        false,
                    ),
                ],
                vec![],
                vec![
                    super::RawPrefetch(
                        "tags".into(),
                        "tags".into(),
                        vec!["id".into(), "label".into()],
                        Some("deleted_at".into()),
                        vec![("id".into(), false)],
                        Some((
                            "article_tags".into(),
                            "hinoki_link".into(),
                            "id".into(),
                            "tag_id".into(),
                            "article_id".into(),
                        )),
                        "hinoki_link.article_id".into(),
                        "hinoki_link__0".into(),
                        vec![
                            ("id".into(), "id".into(), "int".into(), false),
                            ("label".into(), "label".into(), "str".into(), false),
                        ],
                        Some(50),
                        dialect.into(),
                    ),
                    super::RawPrefetch(
                        "comments".into(),
                        "comments".into(),
                        vec!["id".into(), "article_id".into(), "body".into()],
                        None,
                        vec![("id".into(), true)],
                        None,
                        "article_id".into(),
                        "article_id".into(),
                        vec![
                            ("id".into(), "id".into(), "int".into(), false),
                            ("body".into(), "body".into(), "str".into(), false),
                        ],
                        None,
                        dialect.into(),
                    ),
                ],
                Some("id"),
                KeyKind::Int,
            )
            .unwrap();

            for pf in &plan.prefetches {
                assert!(pf.single_owner_template.is_some());
                let (cached_sql, cached_params) = pf.compile_chunk(&[Value::Int(42)]).unwrap();
                assert!(matches!(cached_sql, Cow::Borrowed(_)));
                let (dyn_sql, dyn_params) = pf.compile_chunk_dynamic(&[Value::Int(42)]).unwrap();
                assert_eq!(cached_sql.as_ref(), dyn_sql.as_str(), "dialect={dialect}");
                assert_eq!(cached_params, dyn_params, "dialect={dialect}");

                let (multi_sql, multi_params) =
                    pf.compile_chunk(&[Value::Int(1), Value::Int(2)]).unwrap();
                assert!(matches!(multi_sql, Cow::Owned(_)));
                let (dyn_multi_sql, dyn_multi_params) = pf
                    .compile_chunk_dynamic(&[Value::Int(1), Value::Int(2)])
                    .unwrap();
                assert_eq!(multi_sql.as_ref(), dyn_multi_sql.as_str());
                assert_eq!(multi_params, dyn_multi_params);
            }
        }
    }
}
