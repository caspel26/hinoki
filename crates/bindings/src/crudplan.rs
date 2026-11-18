//! Immutable, startup-compiled metadata for one generated public CRUD route.

use std::sync::Arc;

use hinoki_orm::query::{
    compile_delete_with, compile_insert_with, compile_select_with, compile_update_with, Delete,
    Dialect, Filter, Insert, Op, Select, Update,
};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

use crate::rows::RowPlan;
use crate::schema::{Compiled, NativeSchema};

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
}

#[derive(Clone, Debug)]
pub(crate) enum ModelDefaultValue {
    Required,
    None,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(String),
    Uuid4,
    UtcNow,
}

#[derive(Clone, Debug)]
pub(crate) struct ModelDefault {
    pub(crate) column: String,
    pub(crate) value: ModelDefaultValue,
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
    pub(crate) create_columns: Vec<String>,
    pub(crate) update_columns: Vec<String>,
    pub(crate) read_rows: Arc<RowPlan>,
    templates: Vec<DialectTemplates>,
    validation_error: Vec<u8>,
    _schemas: Vec<Arc<NativeSchema>>,
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
        model_fields, model_defaults, create_defaults, create_columns, update_columns,
        read_schema, read_fields,
        create_schema=None, update_schema=None, validation_error=Vec::new()
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
        create_columns: Vec<String>,
        update_columns: Vec<String>,
        read_schema: Py<Compiled>,
        read_fields: Vec<(String, String, String, bool)>,
        create_schema: Option<Py<Compiled>>,
        update_schema: Option<Py<Compiled>>,
        validation_error: Vec<u8>,
    ) -> PyResult<Self> {
        let schemas = compiled_schemas(py, read_schema, create_schema, update_schema)?;
        let inner = CrudPlan::new(
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
            create_columns,
            update_columns,
            read_fields,
            validation_error,
            schemas,
        )?;
        Ok(Self {
            inner: Arc::new(inner),
        })
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
            ]);
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
                ModelDefaultValue::Uuid4 => ("uuid4", None),
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

    pub(crate) fn list_sql(
        &self,
        dialect_name: &str,
        page_size: u64,
        offset: u64,
    ) -> PyResult<String> {
        let dialect = DIALECTS
            .iter()
            .find_map(|(name, dialect)| (*name == dialect_name).then_some(*dialect))
            .ok_or_else(|| PyValueError::new_err("unsupported CRUD database dialect"))?;
        let order = if self.ordering.is_empty() {
            vec![(self.primary_key.clone(), false)]
        } else {
            self.ordering.clone()
        };
        compile_select_with(
            &Select::<usize> {
                table: self.table.clone(),
                columns: self.columns.clone(),
                joins: Vec::new(),
                filters: Vec::new(),
                order,
                limit: Some(page_size),
                offset: Some(offset),
                count: false,
            },
            dialect,
        )
        .map(|(sql, _)| sql)
        .map_err(query_error)
    }

    pub(crate) fn read_rows(&self) -> &RowPlan {
        &self.read_rows
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
        create_columns: Vec<String>,
        update_columns: Vec<String>,
        read_fields: Vec<(String, String, String, bool)>,
        validation_error: Vec<u8>,
        schemas: Vec<Arc<NativeSchema>>,
    ) -> PyResult<Self> {
        validate_metadata(
            &action,
            &columns,
            &primary_key,
            page_size,
            max_page_size,
            &model_fields,
            &model_defaults,
            &create_defaults,
            &create_columns,
            &update_columns,
        )?;
        validate_error_template(&validation_error)?;
        let read_rows = Arc::new(
            RowPlan::new(columns.clone(), read_fields)
                .map_err(|error| PyValueError::new_err(error.to_string()))?,
        );
        let list_order = if ordering.is_empty() {
            vec![(primary_key.clone(), false)]
        } else {
            ordering.clone()
        };
        let templates = DIALECTS
            .iter()
            .map(|(name, dialect)| {
                compile_templates(
                    name,
                    *dialect,
                    &table,
                    &columns,
                    &primary_key,
                    &ordering,
                    &list_order,
                    page_size,
                    &create_columns,
                    &update_columns,
                )
            })
            .collect::<PyResult<Vec<_>>>()?;
        Ok(Self {
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
            create_columns,
            update_columns,
            read_rows,
            templates,
            validation_error,
            _schemas: schemas,
        })
    }
}

fn compiled_schemas(
    py: Python<'_>,
    read: Py<Compiled>,
    create: Option<Py<Compiled>>,
    update: Option<Py<Compiled>>,
) -> PyResult<Vec<Arc<NativeSchema>>> {
    let mut found = vec![native_schema(py, &read)?];
    for schema in [create, update].into_iter().flatten() {
        found.push(native_schema(py, &schema)?);
    }
    Ok(found)
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
    create_columns: &[String],
    update_columns: &[String],
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
        "uuid4" if value.is_empty() => Ok(ModelDefaultValue::Uuid4),
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

fn validate_model_fields(fields: &[(String, String, bool)], columns: &[String]) -> PyResult<()> {
    let supported = ["str", "int", "float", "bool", "uuid", "datetime"];
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
fn compile_templates(
    name: &str,
    dialect: Dialect,
    table: &str,
    columns: &[String],
    primary_key: &str,
    ordering: &[(String, bool)],
    list_order: &[(String, bool)],
    page_size: u64,
    create_columns: &[String],
    update_columns: &[String],
) -> PyResult<DialectTemplates> {
    let count = select_sql(table, &[], &[], None, None, true, dialect)?;
    let list = select_sql(
        table,
        columns,
        list_order,
        Some(page_size),
        Some(0),
        false,
        dialect,
    )?;
    let retrieve = select_filtered_sql(table, columns, primary_key, ordering, Some(2), dialect)?;
    let create_returning = returning_columns(columns, dialect);
    let create = if create_columns.is_empty() {
        None
    } else {
        Some(
            compile_insert_with(
                &Insert {
                    table: table.to_owned(),
                    assignments: indexed(create_columns),
                    returning: create_returning,
                },
                dialect,
            )
            .map_err(query_error)?
            .0,
        )
    };
    let update = if update_columns.is_empty() {
        None
    } else {
        let mut assignments = indexed(update_columns);
        let filter_value = assignments.len();
        let sql = compile_update_with(
            &Update {
                table: table.to_owned(),
                assignments: std::mem::take(&mut assignments),
                filters: vec![equality_filter(primary_key, filter_value)],
                returning: returning_columns(columns, dialect),
            },
            dialect,
        )
        .map_err(query_error)?
        .0;
        Some(sql)
    };
    let delete = compile_delete_with(
        &Delete {
            table: table.to_owned(),
            filters: vec![equality_filter(primary_key, 0)],
        },
        dialect,
    )
    .map_err(query_error)?
    .0;
    let read_back = select_filtered_sql(table, columns, primary_key, ordering, Some(2), dialect)?;
    Ok(DialectTemplates {
        dialect: name.to_owned(),
        count,
        list,
        retrieve,
        create,
        update,
        delete,
        read_back,
    })
}

fn select_sql(
    table: &str,
    columns: &[String],
    order: &[(String, bool)],
    limit: Option<u64>,
    offset: Option<u64>,
    count: bool,
    dialect: Dialect,
) -> PyResult<String> {
    compile_select_with(
        &Select::<usize> {
            table: table.to_owned(),
            columns: columns.to_vec(),
            joins: Vec::new(),
            filters: Vec::new(),
            order: order.to_vec(),
            limit,
            offset,
            count,
        },
        dialect,
    )
    .map(|(sql, _)| sql)
    .map_err(query_error)
}

fn select_filtered_sql(
    table: &str,
    columns: &[String],
    primary_key: &str,
    order: &[(String, bool)],
    limit: Option<u64>,
    dialect: Dialect,
) -> PyResult<String> {
    compile_select_with(
        &Select::<usize> {
            table: table.to_owned(),
            columns: columns.to_vec(),
            joins: Vec::new(),
            filters: vec![equality_filter(primary_key, 0)],
            order: order.to_vec(),
            limit,
            offset: None,
            count: false,
        },
        dialect,
    )
    .map(|(sql, _)| sql)
    .map_err(query_error)
}

fn equality_filter(column: &str, value: usize) -> Filter<usize> {
    Filter {
        column: column.to_owned(),
        op: Op::Eq,
        values: vec![value],
    }
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
