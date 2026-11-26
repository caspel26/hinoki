//! Immutable, startup-compiled metadata for one generated public CRUD route.

use std::sync::Arc;

use hinoki_orm::query::{
    compile_delete_with, compile_insert_with, compile_select_bound, compile_select_fixed,
    compile_select_with, compile_update_with, Delete, Dialect, Filter, Insert, Op, QueryError,
    Select, Update,
};
use hinoki_orm::Value;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

use crate::filters::{PyFilterPlan, Spec as FilterSpec};
use crate::jwt::{ClaimsAuth, PyClaimsAuth};
use crate::links::PyLinkSpec;
use crate::rows::RowPlan;
use crate::schema::{Compiled, NativeSchema};
use hinoki_orm::links::{KeyKind, LinkTable};

const REQUEST_ID_SENTINEL: &str = "0123456789abcdeffedcba9876543210";
const DIALECTS: [(&str, Dialect); 4] = [
    ("postgres", Dialect::Postgres),
    ("sqlite", Dialect::Sqlite),
    ("mysql", Dialect::Mysql),
    ("mariadb", Dialect::Mysql),
];

#[derive(Clone, Debug)]
struct DialectTemplates {
    dialect: String,
    count: String,
    list: String,
    retrieve: String,
    create: Option<String>,
    update: Option<String>,
    delete: String,
    read_back: String,
    write_read_back: String,
    update_read_back: String,
    savepoint: Option<String>,
    rollback_savepoint: Option<String>,
    release_savepoint: Option<String>,
}

type WriteSchemas = (Option<Arc<NativeSchema>>, Option<Arc<NativeSchema>>);

#[derive(Clone, Debug)]
pub(crate) enum ModelDefaultValue {
    Required,
    None,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(String),
    /// Text a `date` or `Decimal` column stores, already in its stored form.
    Stored(String),
    Uuid4,
    Uuid7,
    /// A URL-safe token of this many random bytes.
    Token(usize),
    UtcNow,
}

#[derive(Clone, Debug)]
pub(crate) struct ModelDefault {
    pub(crate) column: String,
    pub(crate) value: ModelDefaultValue,
}

/// Who a row scope resolves against: the caller's `sub` or one named claim.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum ScopeSource {
    CallerId,
    Claim(String),
}

/// A compiled mandatory row scope on a text column.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct RowScopeSpec {
    pub(crate) column: String,
    pub(crate) source: ScopeSource,
}

/// Caller identity passed from native admission into native CRUD execution.
#[derive(Clone, Copy)]
pub(crate) struct CallerContext<'a> {
    pub(crate) subject: &'a str,
    pub(crate) claims: &'a [(String, Option<String>)],
}

impl RowScopeSpec {
    /// Resolve the required text scope value for `caller`, or `None` when
    /// missing or empty.
    pub(crate) fn resolve(&self, caller: Option<CallerContext<'_>>) -> Option<Value> {
        let caller = caller?;
        let raw = match &self.source {
            ScopeSource::CallerId => caller.subject,
            ScopeSource::Claim(name) => caller
                .claims
                .iter()
                .find(|(key, _)| key == name)
                .and_then(|(_, value)| value.as_deref())?,
        };
        (!raw.is_empty()).then(|| Value::Text(raw.to_owned()))
    }
}

pub(crate) type ScopeSpecArg = (String, String, Option<String>);

/// One many-to-many relation the route reads and writes through its ids field.
#[derive(Clone, Debug)]
pub(crate) struct PlanLink {
    /// The ids field of the schemas.
    pub(crate) field: String,
    pub(crate) table: LinkTable,
    /// Optional target-side row scope, or `None` when every target row is allowed.
    pub(crate) scope: Option<RowScopeSpec>,
}

/// No Python object is retained by a plan; validator snapshots are Rust-owned.
#[allow(
    dead_code,
    reason = "metadata is consumed by native CRUD execution in later steps"
)]
pub(crate) struct CrudPlan {
    pub(crate) action: String,
    pub(crate) model_name: String,
    pub(crate) table: String,
    pub(crate) columns: Vec<String>,
    pub(crate) primary_key: String,
    pub(crate) ordering: Vec<(String, bool)>,
    pub(crate) page_size: u64,
    pub(crate) max_page_size: u64,
    pub(crate) model_fields: Vec<(String, String, bool)>,
    pub(crate) model_defaults: Vec<ModelDefault>,
    pub(crate) create_defaults: Vec<ModelDefault>,
    pub(crate) update_defaults: Vec<ModelDefault>,
    pub(crate) create_columns: Vec<String>,
    pub(crate) update_columns: Vec<String>,
    pub(crate) updated_at: Option<String>,
    pub(crate) read_rows: Arc<RowPlan>,
    /// Claims-only authentication Rust performs before the route; `None` for a public route.
    pub(crate) auth: Option<Arc<ClaimsAuth>>,
    /// Text column holding the caller's id: set on create, and required to
    /// match on update and delete. Only present together with `auth`.
    pub(crate) owner: Option<String>,
    /// Mandatory tenant/row scope applied to every read, write, and reload.
    pub(crate) scope: Option<RowScopeSpec>,
    /// The relations of the ids fields; `read_links[slot]` is the one the
    /// `slot`-th ids field of the read plan shows.
    pub(crate) links: Vec<PlanLink>,
    pub(crate) read_links: Vec<usize>,
    /// Declared client filters for a `list` action; `None` when unfiltered.
    pub(crate) filters: Option<Arc<FilterSpec>>,
    /// The soft-delete column: every read skips rows where it is set, and a
    /// delete sets it to the current time instead of removing the row.
    pub(crate) soft_delete: Option<String>,
    templates: Vec<DialectTemplates>,
    validation_error: Vec<u8>,
    create_schema: Option<Arc<NativeSchema>>,
    update_schema: Option<Arc<NativeSchema>>,
}

#[pyclass(name = "_CrudPlan", module = "hinoki.core", frozen)]
pub(crate) struct PyCrudPlan {
    pub(crate) inner: Arc<CrudPlan>,
}

#[pymethods]
impl PyCrudPlan {
    #[new]
    #[pyo3(signature = (
        action, model_name, table, columns, primary_key, ordering, page_size, max_page_size,
        model_fields, model_defaults, create_defaults, update_defaults, create_columns, update_columns,
        updated_at,
        read_schema, read_fields,
        create_schema=None, update_schema=None, validation_error=Vec::new(), auth=None,
        owner=None, scope=None, links=Vec::new(), filters=None, soft_delete=None
    ))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        py: Python<'_>,
        action: String,
        model_name: String,
        table: String,
        columns: Vec<String>,
        primary_key: String,
        ordering: Vec<(String, bool)>,
        page_size: u64,
        max_page_size: u64,
        model_fields: Vec<(String, String, bool)>,
        model_defaults: Vec<(String, String, String)>,
        create_defaults: Vec<(String, String, String)>,
        update_defaults: Vec<(String, String, String)>,
        create_columns: Vec<String>,
        update_columns: Vec<String>,
        updated_at: Option<String>,
        read_schema: Py<Compiled>,
        read_fields: Vec<(String, String, String, bool)>,
        create_schema: Option<Py<Compiled>>,
        update_schema: Option<Py<Compiled>>,
        validation_error: Vec<u8>,
        auth: Option<Py<PyClaimsAuth>>,
        owner: Option<String>,
        scope: Option<ScopeSpecArg>,
        links: Vec<(String, Py<PyLinkSpec>, Option<ScopeSpecArg>)>,
        filters: Option<Py<PyFilterPlan>>,
        soft_delete: Option<String>,
    ) -> PyResult<Self> {
        if owner.is_some() && auth.is_none() {
            return Err(PyValueError::new_err(
                "an owner column needs native claims authentication",
            ));
        }
        let scope = scope.map(parse_scope_spec).transpose()?;
        if scope.is_some() && auth.is_none() {
            return Err(PyValueError::new_err(
                "a row scope needs native claims authentication",
            ));
        }
        let _ = native_schema(py, &read_schema)?;
        let (create_schema, update_schema) =
            compiled_write_schemas(py, create_schema, update_schema)?;
        let mut inner = CrudPlan::new(
            action,
            model_name,
            table,
            columns,
            primary_key,
            ordering,
            page_size,
            max_page_size,
            model_fields,
            model_defaults,
            create_defaults,
            update_defaults,
            create_columns,
            update_columns,
            updated_at,
            read_fields,
            validation_error,
            create_schema,
            update_schema,
            owner,
            scope,
        )?;
        inner.auth = auth.map(|auth| Arc::clone(&auth.get().inner));
        let parsed_links = links
            .into_iter()
            .map(|(field, spec, scope)| {
                Ok(PlanLink {
                    field,
                    table: spec.get().table.clone(),
                    scope: scope.map(parse_scope_spec).transpose()?,
                })
            })
            .collect::<PyResult<Vec<_>>>()?;
        inner.attach_links(parsed_links)?;
        inner.attach_filters(filters.map(|plan| Arc::clone(&plan.get().spec)))?;
        inner.attach_soft_delete(soft_delete)?;
        Ok(Self {
            inner: Arc::new(inner),
        })
    }

    #[getter]
    fn scope_column(&self) -> Option<&str> {
        self.inner.scope_column()
    }

    #[getter]
    fn action(&self) -> &str {
        &self.inner.action
    }

    #[getter]
    fn table(&self) -> &str {
        &self.inner.table
    }

    #[getter]
    fn dialects(&self) -> Vec<String> {
        self.inner
            .templates
            .iter()
            .map(|template| template.dialect.clone())
            .collect()
    }

    /// The list statement and its bound page window, for differential tests.
    #[pyo3(name = "list_sql")]
    fn py_list_sql(
        &self,
        dialect: &str,
        page_size: u64,
        offset: u64,
    ) -> PyResult<(String, Vec<i64>)> {
        let sentinel = self
            .inner
            .scope
            .as_ref()
            .map(|_| Value::Text(String::new()));
        let (sql, params) = self.inner.list_sql(dialect, page_size, offset, sentinel)?;
        let offset_params = usize::from(self.inner.scope.is_some());
        let counts = params
            .into_iter()
            .skip(offset_params)
            .map(|value| match value {
                Value::Int(count) => Ok(count),
                _ => Err(PyValueError::new_err("list window is not a row count")),
            })
            .collect::<PyResult<_>>()?;
        Ok((sql, counts))
    }

    /// Compiled SQL as `(dialect, kind, sql)` tuples for differential tests.
    fn templates(&self) -> Vec<(String, String, String)> {
        let mut found = Vec::new();
        for template in &self.inner.templates {
            found.extend([
                (
                    template.dialect.clone(),
                    "count".to_owned(),
                    template.count.clone(),
                ),
                (
                    template.dialect.clone(),
                    "list".to_owned(),
                    template.list.clone(),
                ),
                (
                    template.dialect.clone(),
                    "retrieve".to_owned(),
                    template.retrieve.clone(),
                ),
                (
                    template.dialect.clone(),
                    "delete".to_owned(),
                    template.delete.clone(),
                ),
                (
                    template.dialect.clone(),
                    "read_back".to_owned(),
                    template.read_back.clone(),
                ),
                (
                    template.dialect.clone(),
                    "write_read_back".to_owned(),
                    template.write_read_back.clone(),
                ),
                (
                    template.dialect.clone(),
                    "update_read_back".to_owned(),
                    template.update_read_back.clone(),
                ),
            ]);
            for (kind, sql) in [
                ("savepoint", &template.savepoint),
                ("rollback_savepoint", &template.rollback_savepoint),
                ("release_savepoint", &template.release_savepoint),
            ] {
                if let Some(sql) = sql {
                    found.push((template.dialect.clone(), kind.to_owned(), sql.clone()));
                }
            }
            if let Some(sql) = &template.create {
                found.push((template.dialect.clone(), "create".to_owned(), sql.clone()));
            }
            if let Some(sql) = &template.update {
                found.push((template.dialect.clone(), "update".to_owned(), sql.clone()));
            }
        }
        found
    }

    #[getter]
    fn validation_error_template<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.inner.validation_error)
    }

    #[getter]
    fn update_columns(&self) -> Vec<String> {
        self.inner.update_columns.clone()
    }

    /// Rust-owned defaults retained for native create and differential tests.
    fn model_defaults(&self) -> Vec<(String, String, Option<String>)> {
        defaults_to_py(&self.inner.model_defaults)
    }

    #[getter]
    fn create_defaults(&self) -> Vec<(String, String, Option<String>)> {
        defaults_to_py(&self.inner.create_defaults)
    }
}

fn defaults_to_py(values: &[ModelDefault]) -> Vec<(String, String, Option<String>)> {
    values
        .iter()
        .map(|item| {
            let (kind, value) = match &item.value {
                ModelDefaultValue::Required => ("required", None),
                ModelDefaultValue::None => ("none", None),
                ModelDefaultValue::Bool(value) => ("bool", Some(value.to_string())),
                ModelDefaultValue::Int(value) => ("int", Some(value.to_string())),
                ModelDefaultValue::Float(value) => ("float", Some(value.to_string())),
                ModelDefaultValue::Str(value) => ("str", Some(value.clone())),
                ModelDefaultValue::Stored(value) => ("stored", Some(value.clone())),
                ModelDefaultValue::Uuid4 => ("uuid4", None),
                ModelDefaultValue::Uuid7 => ("uuid7", None),
                ModelDefaultValue::Token(count) => ("token", Some(count.to_string())),
                ModelDefaultValue::UtcNow => ("utc_now", None),
            };
            (item.column.clone(), kind.to_owned(), value)
        })
        .collect()
}

#[allow(clippy::too_many_arguments)]
impl CrudPlan {
    pub(crate) fn count_sql(&self, dialect: &str) -> Option<&str> {
        self.template(dialect)
            .map(|template| template.count.as_str())
    }

    pub(crate) fn retrieve_sql(&self, dialect: &str) -> Option<&str> {
        self.template(dialect)
            .map(|template| template.retrieve.as_str())
    }

    pub(crate) fn create_sql(&self, dialect: &str) -> Option<&str> {
        self.template(dialect)?.create.as_deref()
    }

    pub(crate) fn delete_sql(&self, dialect: &str) -> Option<&str> {
        self.template(dialect)
            .map(|template| template.delete.as_str())
    }

    pub(crate) fn read_back_sql(&self, dialect: &str) -> Option<&str> {
        self.template(dialect)
            .map(|template| template.read_back.as_str())
    }

    pub(crate) fn write_read_back_sql(&self, dialect: &str) -> Option<&str> {
        self.template(dialect)
            .map(|template| template.write_read_back.as_str())
    }

    pub(crate) fn update_read_back_sql(&self, dialect: &str) -> Option<&str> {
        self.template(dialect)
            .map(|template| template.update_read_back.as_str())
    }

    pub(crate) fn savepoint_sql(&self, dialect: &str) -> Option<&str> {
        self.template(dialect)?.savepoint.as_deref()
    }

    pub(crate) fn rollback_savepoint_sql(&self, dialect: &str) -> Option<&str> {
        self.template(dialect)?.rollback_savepoint.as_deref()
    }

    pub(crate) fn release_savepoint_sql(&self, dialect: &str) -> Option<&str> {
        self.template(dialect)?.release_savepoint.as_deref()
    }

    pub(crate) fn create_schema(&self) -> Option<&NativeSchema> {
        self.create_schema.as_deref()
    }

    pub(crate) fn update_schema(&self) -> Option<&NativeSchema> {
        self.update_schema.as_deref()
    }

    pub(crate) fn create_columns(&self) -> &[String] {
        &self.create_columns
    }

    pub(crate) fn create_defaults(&self) -> &[ModelDefault] {
        &self.create_defaults
    }

    pub(crate) fn update_defaults(&self) -> &[ModelDefault] {
        &self.update_defaults
    }

    pub(crate) fn model_defaults(&self) -> &[ModelDefault] {
        &self.model_defaults
    }

    pub(crate) fn model_field_kind(&self, column: &str) -> Option<&str> {
        self.model_fields
            .iter()
            .find(|(name, _, _)| name == column)
            .map(|(_, kind, _)| kind.as_str())
    }

    pub(crate) fn scope_column(&self) -> Option<&str> {
        self.scope.as_ref().map(|spec| spec.column.as_str())
    }

    pub(crate) fn update_sql(&self, dialect_name: &str, columns: &[String]) -> PyResult<String> {
        let dialect = DIALECTS
            .iter()
            .find_map(|(name, dialect)| (*name == dialect_name).then_some(*dialect))
            .ok_or_else(|| PyValueError::new_err("unsupported CRUD database dialect"))?;
        if columns.is_empty()
            || columns
                .iter()
                .any(|column| !self.update_columns.contains(column))
        {
            return Err(PyValueError::new_err("invalid CRUD update columns"));
        }
        let assignments = indexed(columns);
        compile_update_with(
            &Update {
                table: self.table.clone(),
                assignments,
                filters: scoped_filters(
                    (self.soft_delete.as_deref(), &self.primary_key),
                    self.scope_column(),
                    self.owner.as_deref(),
                    columns.len(),
                ),
                subfilters: Vec::new(),
                returning: returning_columns(&self.columns, dialect),
            },
            dialect,
        )
        .map(|(sql, _)| sql)
        .map_err(query_error)
    }

    pub(crate) fn list_sql(
        &self,
        dialect_name: &str,
        page_size: u64,
        offset: u64,
        scope_value: Option<Value>,
    ) -> PyResult<(String, Vec<Value>)> {
        let dialect = resolve_dialect(dialect_name)?;
        let order = self.default_list_order();
        let filters = self.with_base_filters(scope_value, Vec::new());
        compile_select_with(
            &Select::<Value> {
                table: self.table.clone(),
                columns: self.columns.clone(),
                joins: Vec::new(),
                filters,
                subfilters: Vec::new(),
                keyset: None,
                order,
                limit: Some(page_size),
                offset: Some(offset),
                count: false,
            },
            dialect,
        )
        .map_err(query_error)
    }

    pub(crate) fn filtered_count_sql(
        &self,
        dialect_name: &str,
        scope_value: Option<Value>,
        filters: Vec<Filter<Value>>,
    ) -> Result<(String, Vec<Value>), QueryError> {
        let dialect = find_dialect(dialect_name)
            .ok_or(QueryError::Unsupported("unsupported CRUD database dialect"))?;
        let filters = self.with_base_filters(scope_value, filters);
        compile_select_with(
            &Select {
                table: self.table.clone(),
                columns: Vec::new(),
                joins: Vec::new(),
                filters,
                subfilters: Vec::new(),
                keyset: None,
                order: Vec::new(),
                limit: None,
                offset: None,
                count: true,
            },
            dialect,
        )
    }

    pub(crate) fn filtered_list_sql(
        &self,
        dialect_name: &str,
        scope_value: Option<Value>,
        filters: Vec<Filter<Value>>,
        order: Vec<(String, bool)>,
        page_size: u64,
        offset: u64,
    ) -> Result<(String, Vec<Value>), QueryError> {
        let dialect = find_dialect(dialect_name)
            .ok_or(QueryError::Unsupported("unsupported CRUD database dialect"))?;
        let order = if order.is_empty() {
            self.default_list_order()
        } else {
            order
        };
        let filters = self.with_base_filters(scope_value, filters);
        compile_select_with(
            &Select {
                table: self.table.clone(),
                columns: self.columns.clone(),
                joins: Vec::new(),
                filters,
                subfilters: Vec::new(),
                keyset: None,
                order,
                limit: Some(page_size),
                offset: Some(offset),
                count: false,
            },
            dialect,
        )
    }

    fn with_base_filters(
        &self,
        scope_value: Option<Value>,
        filters: Vec<Filter<Value>>,
    ) -> Vec<Filter<Value>> {
        let mut scoped = soft_filters(self.soft_delete.as_deref());
        if let (Some(spec), Some(value)) = (&self.scope, scope_value) {
            scoped.push(Filter {
                column: spec.column.clone(),
                op: Op::Eq,
                values: vec![value],
            });
        }
        scoped.extend(filters);
        scoped
    }

    /// Scope the plan to the rows `column` does not mark as deleted. The
    /// column must be a nullable timestamp that no schema writes.
    fn attach_soft_delete(&mut self, column: Option<String>) -> PyResult<()> {
        let Some(column) = column else {
            return Ok(());
        };
        let timestamp = self
            .model_fields
            .iter()
            .any(|(name, kind, nullable)| name == &column && kind == "datetime" && *nullable);
        if !timestamp
            || column == self.primary_key
            || self.update_columns.contains(&column)
            || self.updated_at.as_deref() == Some(column.as_str())
        {
            return Err(PyValueError::new_err(
                "the soft-delete column must be a nullable timestamp no route writes",
            ));
        }
        self.soft_delete = Some(column);
        self.templates = self.compile_dialect_templates()?;
        Ok(())
    }

    fn compile_dialect_templates(&self) -> PyResult<Vec<DialectTemplates>> {
        DIALECTS
            .iter()
            .map(|(name, dialect)| compile_templates(name, *dialect, self))
            .collect()
    }

    /// The assignments of a soft delete: the column, then `updated_at` when
    /// the model keeps one, each bound to the current time.
    /// The columns a soft delete stamps, in bind order; empty for a hard delete.
    pub(crate) fn soft_delete_assignments(&self) -> Vec<String> {
        let Some(soft) = &self.soft_delete else {
            return Vec::new();
        };
        std::iter::once(soft)
            .chain(self.updated_at.iter())
            .cloned()
            .collect()
    }

    fn default_list_order(&self) -> Vec<(String, bool)> {
        if self.ordering.is_empty() {
            vec![(self.primary_key.clone(), false)]
        } else {
            self.ordering.clone()
        }
    }

    pub(crate) fn attach_filters(&mut self, spec: Option<Arc<FilterSpec>>) -> PyResult<()> {
        let Some(spec) = spec else {
            return Ok(());
        };
        if self.action != "list" {
            return Err(PyValueError::new_err(
                "filters apply only to a list CRUD plan",
            ));
        }
        let valid = spec
            .fields
            .iter()
            .map(|field| field.name.as_str())
            .chain(spec.search.iter().map(String::as_str))
            .chain(spec.ordering.iter().map(String::as_str))
            .all(|column| self.columns.iter().any(|known| known == column));
        if !valid {
            return Err(PyValueError::new_err(
                "filter references an unknown model column",
            ));
        }
        self.filters = Some(spec);
        Ok(())
    }

    pub(crate) fn read_rows(&self) -> &RowPlan {
        &self.read_rows
    }

    /// Attach the relations: every ids field of the read plan needs one, and a
    /// route that writes them needs a claims-authenticated owner scope or none.
    fn attach_links(&mut self, links: Vec<PlanLink>) -> PyResult<()> {
        let mut read_links = Vec::new();
        for name in self.read_rows.ids_names() {
            let index = links
                .iter()
                .position(|link| link.field == name)
                .ok_or_else(|| PyValueError::new_err(format!("no relation for {name:?}")))?;
            read_links.push(index);
        }
        for (index, link) in links.iter().enumerate() {
            link.table
                .validate()
                .map_err(|error| PyValueError::new_err(error.to_string()))?;
            if links[..index].iter().any(|other| other.field == link.field) {
                return Err(PyValueError::new_err("duplicate relation field"));
            }
            if link.scope.is_some() && self.auth.is_none() {
                return Err(PyValueError::new_err(
                    "a scoped link needs native claims authentication",
                ));
            }
        }
        self.links = links;
        self.read_links = read_links;
        Ok(())
    }

    fn template(&self, dialect: &str) -> Option<&DialectTemplates> {
        self.templates
            .iter()
            .find(|template| template.dialect == dialect)
    }

    pub(crate) fn new(
        action: String,
        model_name: String,
        table: String,
        columns: Vec<String>,
        primary_key: String,
        ordering: Vec<(String, bool)>,
        page_size: u64,
        max_page_size: u64,
        model_fields: Vec<(String, String, bool)>,
        model_defaults: Vec<(String, String, String)>,
        create_defaults: Vec<(String, String, String)>,
        update_defaults: Vec<(String, String, String)>,
        create_columns: Vec<String>,
        update_columns: Vec<String>,
        updated_at: Option<String>,
        read_fields: Vec<(String, String, String, bool)>,
        validation_error: Vec<u8>,
        create_schema: Option<Arc<NativeSchema>>,
        update_schema: Option<Arc<NativeSchema>>,
        owner: Option<String>,
        scope: Option<RowScopeSpec>,
    ) -> PyResult<Self> {
        validate_owner(
            owner.as_deref(),
            &primary_key,
            &model_fields,
            &update_columns,
        )?;
        validate_scope(
            scope.as_ref(),
            owner.as_deref(),
            &primary_key,
            &model_fields,
            &update_columns,
        )?;
        validate_metadata(
            &action,
            &columns,
            &primary_key,
            page_size,
            max_page_size,
            &model_fields,
            &model_defaults,
            &create_defaults,
            &update_defaults,
            &create_columns,
            &update_columns,
            updated_at.as_deref(),
        )?;
        validate_error_template(&validation_error)?;
        let key_kind = match model_fields
            .iter()
            .find(|(name, _, _)| name == &primary_key)
            .map(|(_, kind, _)| kind.as_str())
        {
            Some("uuid") => KeyKind::Uuid,
            Some("str") => KeyKind::Text {
                max_length: usize::MAX,
            },
            _ => KeyKind::Int,
        };
        let read_rows = Arc::new(
            RowPlan::with_links(columns.clone(), read_fields, (&primary_key, key_kind))
                .map_err(|error| PyValueError::new_err(error.to_string()))?,
        );
        let mut plan = Self {
            action,
            model_name,
            table,
            columns,
            primary_key,
            ordering,
            page_size,
            max_page_size,
            model_fields,
            model_defaults: parse_model_defaults(model_defaults)?,
            create_defaults: parse_model_defaults(create_defaults)?,
            update_defaults: parse_model_defaults(update_defaults)?,
            create_columns,
            update_columns,
            updated_at,
            read_rows,
            auth: None,
            owner,
            scope,
            links: Vec::new(),
            read_links: Vec::new(),
            filters: None,
            soft_delete: None,
            templates: Vec::new(),
            validation_error,
            create_schema,
            update_schema,
        };
        plan.templates = plan.compile_dialect_templates()?;
        Ok(plan)
    }
}

fn find_dialect(dialect_name: &str) -> Option<Dialect> {
    DIALECTS
        .iter()
        .find_map(|(name, dialect)| (*name == dialect_name).then_some(*dialect))
}

fn resolve_dialect(dialect_name: &str) -> PyResult<Dialect> {
    find_dialect(dialect_name)
        .ok_or_else(|| PyValueError::new_err("unsupported CRUD database dialect"))
}

fn compiled_write_schemas(
    py: Python<'_>,
    create: Option<Py<Compiled>>,
    update: Option<Py<Compiled>>,
) -> PyResult<WriteSchemas> {
    Ok((
        create
            .as_ref()
            .map(|schema| native_schema(py, schema))
            .transpose()?,
        update
            .as_ref()
            .map(|schema| native_schema(py, schema))
            .transpose()?,
    ))
}

fn native_schema(py: Python<'_>, schema: &Py<Compiled>) -> PyResult<Arc<NativeSchema>> {
    schema
        .borrow(py)
        .native_shared()
        .ok_or_else(|| PyValueError::new_err("CRUD schema cannot be lowered to Rust"))
}

#[allow(clippy::too_many_arguments)]
fn validate_metadata(
    action: &str,
    columns: &[String],
    primary_key: &str,
    page_size: u64,
    max_page_size: u64,
    model_fields: &[(String, String, bool)],
    model_defaults: &[(String, String, String)],
    create_defaults: &[(String, String, String)],
    update_defaults: &[(String, String, String)],
    create_columns: &[String],
    update_columns: &[String],
    updated_at: Option<&str>,
) -> PyResult<()> {
    if !matches!(action, "list" | "retrieve" | "create" | "update" | "delete") {
        return Err(PyValueError::new_err("unknown generated CRUD action"));
    }
    let names: std::collections::HashSet<_> = columns.iter().collect();
    if names.len() != columns.len() || !names.contains(&primary_key.to_owned()) {
        return Err(PyValueError::new_err("invalid CRUD model columns"));
    }
    if page_size == 0 || max_page_size < page_size {
        return Err(PyValueError::new_err("invalid CRUD page limits"));
    }
    validate_column_subset(create_columns, &names)?;
    validate_column_subset(update_columns, &names)?;
    validate_model_fields(model_fields, columns)?;
    validate_model_defaults(model_defaults, &names)?;
    validate_model_defaults(create_defaults, &names)?;
    validate_model_defaults(update_defaults, &names)?;
    if updated_at.is_some_and(|column| !update_columns.iter().any(|known| known == column)) {
        return Err(PyValueError::new_err("invalid CRUD updated-at column"));
    }
    Ok(())
}

fn validate_model_defaults(
    defaults: &[(String, String, String)],
    columns: &std::collections::HashSet<&String>,
) -> PyResult<()> {
    let names: std::collections::HashSet<_> = defaults.iter().map(|(name, _, _)| name).collect();
    if names.len() != defaults.len() || defaults.iter().any(|(name, _, _)| !columns.contains(name))
    {
        return Err(PyValueError::new_err("invalid CRUD model defaults"));
    }
    for (_, kind, value) in defaults {
        parse_model_default(kind, value)?;
    }
    Ok(())
}

fn parse_model_defaults(defaults: Vec<(String, String, String)>) -> PyResult<Vec<ModelDefault>> {
    defaults
        .into_iter()
        .map(|(column, kind, value)| {
            Ok(ModelDefault {
                column,
                value: parse_model_default(&kind, &value)?,
            })
        })
        .collect()
}

fn parse_model_default(kind: &str, value: &str) -> PyResult<ModelDefaultValue> {
    let invalid = || PyValueError::new_err("invalid CRUD model default");
    match kind {
        "required" if value.is_empty() => Ok(ModelDefaultValue::Required),
        "none" if value.is_empty() => Ok(ModelDefaultValue::None),
        "bool" => match value {
            "true" => Ok(ModelDefaultValue::Bool(true)),
            "false" => Ok(ModelDefaultValue::Bool(false)),
            _ => Err(invalid()),
        },
        "int" => value
            .parse()
            .map(ModelDefaultValue::Int)
            .map_err(|_| invalid()),
        "float" => {
            let parsed: f64 = value.parse().map_err(|_| invalid())?;
            if parsed.is_finite() {
                Ok(ModelDefaultValue::Float(parsed))
            } else {
                Err(invalid())
            }
        }
        "str" => Ok(ModelDefaultValue::Str(value.to_owned())),
        "decimal" if is_stored_decimal(value) => Ok(ModelDefaultValue::Stored(value.to_owned())),
        "date" if crate::schema::parse_date(value).is_some() => {
            Ok(ModelDefaultValue::Stored(value.to_owned()))
        }
        "uuid4" if value.is_empty() => Ok(ModelDefaultValue::Uuid4),
        "uuid7" if value.is_empty() => Ok(ModelDefaultValue::Uuid7),
        "token" => match value.parse::<usize>() {
            Ok(count) if (1..=crate::crypto::MAX_RANDOM).contains(&count) => {
                Ok(ModelDefaultValue::Token(count))
            }
            _ => Err(invalid()),
        },
        "utc_now" if value.is_empty() => Ok(ModelDefaultValue::UtcNow),
        _ => Err(invalid()),
    }
}

fn validate_column_subset(
    values: &[String],
    columns: &std::collections::HashSet<&String>,
) -> PyResult<()> {
    let names: std::collections::HashSet<_> = values.iter().collect();
    if names.len() != values.len() || values.iter().any(|name| !columns.contains(name)) {
        return Err(PyValueError::new_err("invalid CRUD writable columns"));
    }
    Ok(())
}

/// Whether `text` is the stored form Python writes for a finite `Decimal`:
/// `str(value)` without an exponent, so no leading zeros and an adjusted
/// exponent of at least -6.
pub(crate) fn is_stored_decimal(text: &str) -> bool {
    let unsigned = text.strip_prefix('-').unwrap_or(text);
    let (whole, fraction) = unsigned.split_once('.').unwrap_or((unsigned, ""));
    let digits = |part: &str| part.bytes().all(|byte| byte.is_ascii_digit());
    if whole.is_empty()
        || !digits(whole)
        || !digits(fraction)
        || (unsigned.contains('.') && fraction.is_empty())
        || (whole.len() > 1 && whole.starts_with('0'))
    {
        return false;
    }
    let significant = format!("{whole}{fraction}");
    let significant = significant.trim_start_matches('0');
    let length = i64::try_from(significant.len().max(1)).unwrap_or(i64::MAX);
    let places = i64::try_from(fraction.len()).unwrap_or(i64::MAX);
    length - 1 - places >= -6
}

fn validate_model_fields(fields: &[(String, String, bool)], columns: &[String]) -> PyResult<()> {
    let supported = [
        "str", "int", "float", "bool", "uuid", "datetime", "bytes", "decimal", "date",
    ];
    let names: std::collections::HashSet<_> = fields.iter().map(|(name, _, _)| name).collect();
    if fields.len() != columns.len()
        || names.len() != fields.len()
        || columns.iter().any(|name| !names.contains(name))
        || fields
            .iter()
            .any(|(_, kind, _)| !supported.contains(&kind.as_str()))
    {
        return Err(PyValueError::new_err("invalid CRUD model field metadata"));
    }
    Ok(())
}

fn validate_error_template(template: &[u8]) -> PyResult<()> {
    let text = std::str::from_utf8(template)
        .map_err(|_| PyValueError::new_err("CRUD validation template is not UTF-8"))?;
    if text.matches(REQUEST_ID_SENTINEL).count() != 1 {
        return Err(PyValueError::new_err(
            "CRUD validation template must have one request id marker",
        ));
    }
    let parsed: serde_json::Value = serde_json::from_slice(template)
        .map_err(|_| PyValueError::new_err("CRUD validation template is not JSON"))?;
    let valid = parsed["error"] == "validation_error"
        && parsed["request_id"] == REQUEST_ID_SENTINEL
        && parsed["details"].as_array().is_some_and(|details| {
            details.first().is_some_and(|detail| {
                detail["path"].is_string()
                    && detail["code"].is_string()
                    && detail["message"].is_string()
            })
        });
    if !valid {
        return Err(PyValueError::new_err(
            "CRUD validation template does not match the framework error shape",
        ));
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn compile_templates(name: &str, dialect: Dialect, plan: &CrudPlan) -> PyResult<DialectTemplates> {
    let (table, columns, ordering) = (plan.table.as_str(), &plan.columns, &plan.ordering);
    let soft = plan.soft_delete.as_deref();
    let key = (soft, plan.primary_key.as_str());
    let scope = plan.scope_column();
    let owner = plan.owner.as_deref();
    let count = select_sql(table, &[], (soft, scope, &[]), (None, None), true, dialect)?;
    let list = select_sql(
        table,
        columns,
        (soft, scope, &plan.default_list_order()),
        (Some(plan.page_size), Some(0)),
        false,
        dialect,
    )?;
    let retrieve = select_filtered_sql(table, columns, key, scope, ordering, dialect)?;
    let create = create_template(plan, dialect)?;
    let update = if plan.update_columns.is_empty() {
        None
    } else {
        Some(update_template(
            table,
            &plan.update_columns,
            (key, scope, owner),
            columns,
            dialect,
        )?)
    };
    let delete = delete_template(plan, dialect)?;
    let read_back = select_filtered_sql(table, columns, key, scope, ordering, dialect)?;
    let write_read_back = select_write_sql(
        table,
        columns,
        ((None, &plan.primary_key), None, None),
        dialect,
    )?;
    let mut update_read_back = select_write_sql(table, columns, (key, scope, owner), dialect)?;
    if dialect == Dialect::Mysql {
        // A current read: a plain read would keep the handler's snapshot and
        // return the row even when a concurrent owner change matched nothing.
        update_read_back.push_str(" FOR UPDATE");
    }
    Ok(DialectTemplates {
        dialect: name.to_owned(),
        count,
        list,
        retrieve,
        create,
        update,
        delete,
        read_back,
        write_read_back,
        update_read_back,
        savepoint: (dialect == Dialect::Mysql).then(|| "SAVEPOINT hinoki_sp_1".to_owned()),
        rollback_savepoint: (dialect == Dialect::Mysql)
            .then(|| "ROLLBACK TO SAVEPOINT hinoki_sp_1".to_owned()),
        release_savepoint: (dialect == Dialect::Mysql)
            .then(|| "RELEASE SAVEPOINT hinoki_sp_1".to_owned()),
    })
}

fn create_template(plan: &CrudPlan, dialect: Dialect) -> PyResult<Option<String>> {
    if plan.create_columns.is_empty() {
        return Ok(None);
    }
    compile_insert_with(
        &Insert {
            table: plan.table.clone(),
            assignments: indexed(&plan.create_columns),
            returning: returning_columns(&plan.columns, dialect),
        },
        dialect,
    )
    .map(|(sql, _)| Some(sql))
    .map_err(query_error)
}

type KeyScope<'a> = ((Option<&'a str>, &'a str), Option<&'a str>, Option<&'a str>);

fn update_template(
    table: &str,
    assignments: &[String],
    (key, scope, owner): KeyScope<'_>,
    returning: &[String],
    dialect: Dialect,
) -> PyResult<String> {
    compile_update_with(
        &Update {
            table: table.to_owned(),
            assignments: indexed(assignments),
            filters: scoped_filters(key, scope, owner, assignments.len()),
            subfilters: Vec::new(),
            returning: returning_columns(returning, dialect),
        },
        dialect,
    )
    .map(|(sql, _)| sql)
    .map_err(query_error)
}

/// A `DELETE` by key, scope and owner, or for a soft-delete model the `UPDATE`
/// Python's `adelete` runs: the timestamps first, then the key, the scope, the
/// owner and the soft-delete condition once more.
fn delete_template(plan: &CrudPlan, dialect: Dialect) -> PyResult<String> {
    let soft = plan.soft_delete.as_deref();
    let key = (soft, plan.primary_key.as_str());
    let scope = plan.scope_column();
    let owner = plan.owner.as_deref();
    if soft.is_none() {
        return compile_delete_with(
            &Delete {
                table: plan.table.clone(),
                filters: scoped_filters(key, scope, owner, 0),
                subfilters: Vec::new(),
                returning: Vec::new(),
            },
            dialect,
        )
        .map(|(sql, _)| sql)
        .map_err(query_error);
    }
    let assignments = plan.soft_delete_assignments();
    let mut filters = scoped_filters(key, scope, owner, assignments.len());
    filters.extend(soft_filters(soft));
    compile_update_with(
        &Update {
            table: plan.table.clone(),
            assignments: indexed(&assignments),
            filters,
            subfilters: Vec::new(),
            returning: Vec::new(),
        },
        dialect,
    )
    .map(|(sql, _)| sql)
    .map_err(query_error)
}

fn select_write_sql(
    table: &str,
    columns: &[String],
    (key, scope, owner): KeyScope<'_>,
    dialect: Dialect,
) -> PyResult<String> {
    compile_select_fixed(
        &Select::<usize> {
            table: table.to_owned(),
            columns: columns.to_vec(),
            joins: Vec::new(),
            filters: scoped_filters(key, scope, owner, 0),
            subfilters: Vec::new(),
            keyset: None,
            order: Vec::new(),
            limit: None,
            offset: None,
            count: false,
        },
        dialect,
    )
    .map(|(sql, _)| sql)
    .map_err(query_error)
}

fn select_sql(
    table: &str,
    columns: &[String],
    (soft, scope, order): (Option<&str>, Option<&str>, &[(String, bool)]),
    (limit, offset): (Option<u64>, Option<u64>),
    count: bool,
    dialect: Dialect,
) -> PyResult<String> {
    let mut filters = soft_filters(soft);
    if let Some(column) = scope {
        filters.push(equality_filter(column, 0));
    }
    compile_select_bound(
        &Select::<usize> {
            table: table.to_owned(),
            columns: columns.to_vec(),
            joins: Vec::new(),
            filters,
            subfilters: Vec::new(),
            keyset: None,
            order: order.to_vec(),
            limit,
            offset,
            count,
        },
        dialect,
        &mut |value| usize::try_from(value).unwrap_or(0),
    )
    .map(|(sql, _)| sql)
    .map_err(query_error)
}

/// The one-row read by primary key (and optional scope), bound like the Python
/// lane: the key, optional scope, then the [`ONE_ROW_LIMIT`] count.
fn select_filtered_sql(
    table: &str,
    columns: &[String],
    key: (Option<&str>, &str),
    scope: Option<&str>,
    order: &[(String, bool)],
    dialect: Dialect,
) -> PyResult<String> {
    let limit_param = if scope.is_some() { 2 } else { 1 };
    compile_select_bound(
        &Select::<usize> {
            table: table.to_owned(),
            columns: columns.to_vec(),
            joins: Vec::new(),
            filters: scoped_filters(key, scope, None, 0),
            subfilters: Vec::new(),
            keyset: None,
            order: order.to_vec(),
            limit: Some(ONE_ROW_LIMIT.unsigned_abs()),
            offset: None,
            count: false,
        },
        dialect,
        &mut |_| limit_param,
    )
    .map(|(sql, _)| sql)
    .map_err(query_error)
}

/// Rows a one-row read asks for: a second row reveals a non-unique key.
const ONE_ROW_LIMIT: i64 = 2;

/// Parameters of the retrieve and read-back statements for `id` and optional `scope`.
pub(crate) fn retrieve_params(id: Value, scope: Option<&Value>) -> Vec<Value> {
    let mut params = vec![id];
    if let Some(value) = scope {
        params.push(value.clone());
    }
    params.push(Value::Int(ONE_ROW_LIMIT));
    params
}

fn equality_filter(column: &str, value: usize) -> Filter<usize> {
    Filter {
        column: column.to_owned(),
        op: Op::Eq,
        values: vec![value],
    }
}

/// The soft-delete condition `column IS NULL`, when the model has one.
fn soft_filters<V>(column: Option<&str>) -> Vec<Filter<V>> {
    column
        .map(|column| Filter {
            column: column.to_owned(),
            op: Op::IsNull,
            values: Vec::new(),
        })
        .into_iter()
        .collect()
}

/// The soft-delete condition, the key filter, optional scope filter, then optional
/// owner filter in bind order.
fn scoped_filters(
    (soft, primary_key): (Option<&str>, &str),
    scope: Option<&str>,
    owner: Option<&str>,
    first: usize,
) -> Vec<Filter<usize>> {
    let mut filters = soft_filters(soft);
    filters.push(equality_filter(primary_key, first));
    let mut next = first + 1;
    if let Some(column) = scope {
        filters.push(equality_filter(column, next));
        next += 1;
    }
    if let Some(column) = owner {
        filters.push(equality_filter(column, next));
    }
    filters
}

fn parse_scope_spec((column, kind, claim): ScopeSpecArg) -> PyResult<RowScopeSpec> {
    let source = match (kind.as_str(), claim) {
        ("caller", None) => ScopeSource::CallerId,
        ("claim", Some(name)) if !name.is_empty() => ScopeSource::Claim(name),
        _ => return Err(PyValueError::new_err("invalid row scope specification")),
    };
    Ok(RowScopeSpec { column, source })
}

fn validate_scope(
    scope: Option<&RowScopeSpec>,
    owner: Option<&str>,
    primary_key: &str,
    model_fields: &[(String, String, bool)],
    update_columns: &[String],
) -> PyResult<()> {
    let Some(spec) = scope else {
        return Ok(());
    };
    let text = model_fields
        .iter()
        .any(|(name, kind, _)| name == &spec.column && kind == "str");
    if spec.column == primary_key || !text || update_columns.iter().any(|name| name == &spec.column)
    {
        return Err(PyValueError::new_err(
            "the row scope must be a text column that is neither the key nor updatable",
        ));
    }
    if owner == Some(spec.column.as_str()) {
        return Err(PyValueError::new_err(
            "the row scope column cannot match the owner column",
        ));
    }
    Ok(())
}

fn validate_owner(
    owner: Option<&str>,
    primary_key: &str,
    model_fields: &[(String, String, bool)],
    update_columns: &[String],
) -> PyResult<()> {
    let Some(column) = owner else {
        return Ok(());
    };
    let text = model_fields
        .iter()
        .any(|(name, kind, _)| name == column && kind == "str");
    if column == primary_key || !text || update_columns.iter().any(|name| name == column) {
        return Err(PyValueError::new_err(
            "the owner must be a text column that is neither the key nor updatable",
        ));
    }
    Ok(())
}

fn indexed(columns: &[String]) -> Vec<(String, usize)> {
    columns
        .iter()
        .enumerate()
        .map(|(index, name)| (name.clone(), index))
        .collect()
}

fn returning_columns(columns: &[String], dialect: Dialect) -> Vec<String> {
    if dialect == Dialect::Mysql {
        Vec::new()
    } else {
        columns.to_vec()
    }
}

fn query_error(error: hinoki_orm::query::QueryError) -> PyErr {
    PyValueError::new_err(error.to_string())
}

#[cfg(test)]
#[path = "crudplan_tests.rs"]
mod tests;
