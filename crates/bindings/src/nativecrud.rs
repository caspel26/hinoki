//! GIL-free execution for eligible generated CRUD routes.

use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::time::Duration;

use hinoki_orm::{Database, DbError, ErrorKind, Transaction, Value};
use time::{OffsetDateTime, UtcOffset};
use uuid::Uuid;

use crate::crudplan::{CrudPlan, ModelDefault, ModelDefaultValue};
use crate::request::{decode_json_document, decode_path_parameter, parse_query, Json};
use crate::schema::{NativeSchema, Options, Validated};
use crate::server::PreparedRequest;

#[derive(Debug)]
pub(crate) enum NativeRead {
    Reply { status: u16, body: Vec<u8> },
    Rows(Vec<Vec<(String, Value)>>, Option<(u64, u64, u64)>),
    Failure(DbError),
    NotFound,
    Timeout,
    Defer,
}

#[derive(Debug)]
pub(crate) enum NativeWrite {
    Reply { status: u16, body: Vec<u8> },
    Failure(DbError),
    NotFound(String),
    Timeout,
    Defer(Option<Vec<u8>>),
}

#[derive(Debug)]
enum TransactionResult {
    Reply { status: u16, body: Vec<u8> },
    NotFound(String),
    Forbidden(Vec<u8>),
    Replay,
}

enum PreparedWrite {
    Create(Vec<(String, Value)>),
    Update {
        id: Value,
        values: Vec<(String, Value)>,
    },
    Delete(Value),
}

#[derive(serde::Serialize)]
struct ErrorBody<'a> {
    error: &'a str,
    request_id: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    message: Option<&'a str>,
}

#[derive(serde::Serialize)]
struct ValidationBody {
    error: &'static str,
    request_id: String,
    details: Vec<ValidationDetail>,
}

#[derive(serde::Serialize)]
struct ValidationDetail {
    path: String,
    code: &'static str,
    message: String,
}

fn error_body(request_id: &str, code: &'static str, message: Option<&str>) -> Option<Vec<u8>> {
    serde_json::to_vec(&ErrorBody {
        error: code,
        request_id,
        message,
    })
    .ok()
}

fn validation_body(request_id: &str, problems: Vec<crate::schema::Problem>) -> Option<Vec<u8>> {
    let details = problems
        .into_iter()
        .map(|(path, code, message)| ValidationDetail {
            path: if path.is_empty() {
                "data".to_owned()
            } else {
                format!("data.{path}")
            },
            code,
            message,
        })
        .collect();
    serde_json::to_vec(&ValidationBody {
        error: "validation_error",
        request_id: request_id.to_owned(),
        details,
    })
    .ok()
}

type FetchFuture<'a> =
    Pin<Box<dyn Future<Output = Result<Vec<Vec<(String, Value)>>, DbError>> + Send + 'a>>;

trait ReadDatabase {
    fn dialect(&self) -> &str;
    fn fetch<'a>(&'a self, sql: &'a str, values: &'a [Value]) -> FetchFuture<'a>;
}

impl ReadDatabase for Database {
    fn dialect(&self) -> &str {
        Database::dialect(self)
    }

    fn fetch<'a>(&'a self, sql: &'a str, values: &'a [Value]) -> FetchFuture<'a> {
        Box::pin(Database::fetch(self, sql, values))
    }
}

#[derive(Clone, Copy)]
struct Page {
    number: u64,
    size: u64,
    offset: u64,
}

pub(crate) async fn execute_read(
    plan: &CrudPlan,
    database: &Database,
    path_params: &[(String, String)],
    raw_query: &[u8],
) -> NativeRead {
    execute_read_with(plan, database, path_params, raw_query).await
}

pub(crate) async fn execute_write(
    plan: &CrudPlan,
    database: &Database,
    request: &PreparedRequest,
    path_params: &[(String, String)],
    raw_query: &[u8],
    max_body_bytes: usize,
    timeout: Duration,
) -> NativeWrite {
    let deadline = tokio::time::Instant::now() + timeout;
    let body = match read_write_body(plan, request, max_body_bytes, deadline).await {
        Ok(body) => body,
        Err(NativeWrite::Timeout) => return http_error(request, 504, "timeout", None),
        Err(result) => return result,
    };
    let prepared = match prepare_write(plan, request, path_params, raw_query, body.as_deref()) {
        Ok(prepared) => prepared,
        Err(result) => return result,
    };
    let transaction = match tokio::time::timeout_at(deadline, database.begin()).await {
        Ok(Ok(transaction)) => transaction,
        Ok(Err(error)) => return NativeWrite::Failure(error),
        Err(_) => return http_error(request, 504, "timeout", None),
    };
    match execute_transaction(
        plan,
        database.dialect(),
        transaction,
        prepared,
        body,
        request.request_id_str(),
        deadline,
    )
    .await
    {
        NativeWrite::Timeout => http_error(request, 504, "timeout", None),
        result => result,
    }
}

async fn read_write_body(
    plan: &CrudPlan,
    request: &PreparedRequest,
    limit: usize,
    deadline: tokio::time::Instant,
) -> Result<Option<Vec<u8>>, NativeWrite> {
    if plan.action == "delete" {
        return Ok(None);
    }
    if let Some(length) = request.header_value(b"content-length") {
        if length.is_ascii()
            && length.iter().all(u8::is_ascii_digit)
            && std::str::from_utf8(&length)
                .ok()
                .and_then(|value| value.parse::<u128>().ok())
                .is_some_and(|value| value > limit as u128)
        {
            return Err(http_error(request, 413, "payload_too_large", None));
        }
    }
    let body = request
        .take_native_body()
        .map_err(|_| NativeWrite::Defer(None))?;
    match tokio::time::timeout_at(deadline, body.read(limit)).await {
        Ok(Ok(bytes)) => Ok(Some(bytes)),
        Ok(Err(hinoki_server::BodyError::TooLarge)) => {
            Err(http_error(request, 413, "payload_too_large", None))
        }
        Ok(Err(hinoki_server::BodyError::Timeout)) => {
            Err(http_error(request, 408, "request_timeout", None))
        }
        Ok(Err(hinoki_server::BodyError::Malformed)) => Err(http_error(
            request,
            400,
            "disconnected",
            Some("the request body is malformed"),
        )),
        Ok(Err(hinoki_server::BodyError::Disconnected)) => Err(http_error(
            request,
            400,
            "disconnected",
            Some("the client disconnected"),
        )),
        Err(_) => Err(NativeWrite::Timeout),
    }
}

fn http_error(
    request: &PreparedRequest,
    status: u16,
    code: &'static str,
    message: Option<&str>,
) -> NativeWrite {
    error_body(request.request_id_str(), code, message)
        .map(|body| NativeWrite::Reply { status, body })
        .unwrap_or(NativeWrite::Defer(None))
}

fn prepare_write(
    plan: &CrudPlan,
    request: &PreparedRequest,
    path_params: &[(String, String)],
    raw_query: &[u8],
    body: Option<&[u8]>,
) -> Result<PreparedWrite, NativeWrite> {
    if parse_query(raw_query, 1000).is_err() {
        return Err(http_error(request, 400, "invalid_query", None));
    }
    match plan.action.as_str() {
        "create" => prepare_create(plan, request, body.ok_or(NativeWrite::Defer(None))?),
        "update" => prepare_update(
            plan,
            request,
            path_params,
            body.ok_or(NativeWrite::Defer(None))?,
        ),
        "delete" => {
            let id = write_path_id(plan, path_params).ok_or(NativeWrite::Defer(None))?;
            Ok(PreparedWrite::Delete(id))
        }
        _ => Err(NativeWrite::Defer(body.map(<[u8]>::to_vec))),
    }
}

fn prepare_create(
    plan: &CrudPlan,
    request: &PreparedRequest,
    body: &[u8],
) -> Result<PreparedWrite, NativeWrite> {
    let json = decode_body(request, body)?;
    let schema = plan
        .create_schema()
        .ok_or(NativeWrite::Defer(Some(body.to_vec())))?;
    let values = validate_body(plan, schema, &json, request)?;
    let Some(mut values) = values else {
        return Err(NativeWrite::Defer(Some(body.to_vec())));
    };
    append_defaults(&mut values, plan.create_defaults(), plan);
    order_schema_values(&mut values, schema);
    ensure_create_requirements(plan, &values, body)?;
    Ok(PreparedWrite::Create(values))
}

fn prepare_update(
    plan: &CrudPlan,
    request: &PreparedRequest,
    path_params: &[(String, String)],
    body: &[u8],
) -> Result<PreparedWrite, NativeWrite> {
    let id = write_path_id(plan, path_params).ok_or(NativeWrite::Defer(Some(body.to_vec())))?;
    let json = decode_body(request, body)?;
    let schema = plan
        .update_schema()
        .ok_or(NativeWrite::Defer(Some(body.to_vec())))?;
    let Some(mut values) = validate_body(plan, schema, &json, request)? else {
        return Err(NativeWrite::Defer(Some(body.to_vec())));
    };
    append_defaults(&mut values, plan.update_defaults(), plan);
    order_schema_values(&mut values, schema);
    Ok(PreparedWrite::Update { id, values })
}

fn decode_body(request: &PreparedRequest, body: &[u8]) -> Result<Json, NativeWrite> {
    let raw = request.header_value(b"content-type").unwrap_or_default();
    let content_type: String = raw.iter().map(|byte| char::from(*byte)).collect();
    if content_type.contains(',') {
        return Err(http_error(
            request,
            400,
            "invalid_content_type",
            Some("several Content-Type values"),
        ));
    }
    let media_type = content_type
        .split(';')
        .next()
        .unwrap_or_default()
        .trim()
        .to_ascii_lowercase();
    if media_type != "application/json" {
        return Err(http_error(
            request,
            415,
            "unsupported_media_type",
            Some("expected application/json"),
        ));
    }
    match decode_json_document(body) {
        Ok(document) if !document.big_integers => Ok(document.root),
        Ok(_) => Err(NativeWrite::Defer(Some(body.to_vec()))),
        Err(_) => Err(http_error(
            request,
            400,
            "invalid_json",
            Some("the body is not valid JSON"),
        )),
    }
}

fn validate_body(
    plan: &CrudPlan,
    schema: &NativeSchema,
    json: &Json,
    request: &PreparedRequest,
) -> Result<Option<Vec<(String, Value)>>, NativeWrite> {
    let options = Options {
        construct: false,
        use_aliases: true,
        allow_naive_datetime: false,
        skip_fields: std::collections::HashSet::new(),
    };
    let (validated, problems) = match schema.validate(json, &options) {
        Ok(result) => result,
        Err(_) => return Ok(None),
    };
    if !problems.is_empty() {
        return Err(validation_body(request.request_id_str(), problems)
            .map(|body| NativeWrite::Reply { status: 422, body })
            .unwrap_or(NativeWrite::Defer(None)));
    }
    let Some(Validated::Object(fields)) = validated else {
        return Ok(None);
    };
    let mut values = Vec::with_capacity(fields.len());
    for (name, value) in fields {
        let Some(kind) = plan.model_field_kind(&name) else {
            return Ok(None);
        };
        let Some(value) = validated_value(kind, value) else {
            return Ok(None);
        };
        values.push((name, value));
    }
    Ok(Some(values))
}

fn order_schema_values(values: &mut [(String, Value)], schema: &NativeSchema) {
    let order = schema.field_names();
    values.sort_by_key(|(name, _)| {
        order
            .iter()
            .position(|field| field == name)
            .unwrap_or(usize::MAX)
    });
}

fn ensure_create_requirements(
    plan: &CrudPlan,
    values: &[(String, Value)],
    body: &[u8],
) -> Result<(), NativeWrite> {
    for column in plan.create_columns() {
        if values.iter().any(|(name, _)| name == column) {
            continue;
        }
        if plan
            .model_defaults()
            .iter()
            .find(|default| &default.column == column)
            .is_none_or(|default| matches!(default.value, ModelDefaultValue::Required))
        {
            return Err(NativeWrite::Defer(Some(body.to_vec())));
        }
    }
    Ok(())
}

fn append_defaults(values: &mut Vec<(String, Value)>, defaults: &[ModelDefault], plan: &CrudPlan) {
    for default in defaults {
        if values.iter().any(|(name, _)| name == &default.column) {
            continue;
        }
        if let Some(value) = default_value(default, plan.model_field_kind(&default.column)) {
            values.push((default.column.clone(), value));
        }
    }
}

fn default_value(default: &ModelDefault, kind: Option<&str>) -> Option<Value> {
    let value = match &default.value {
        ModelDefaultValue::Required => return None,
        ModelDefaultValue::None => Value::Null,
        ModelDefaultValue::Bool(value) => Value::Bool(*value),
        ModelDefaultValue::Int(value) => Value::Int(*value),
        ModelDefaultValue::Float(value) => Value::Float(*value),
        ModelDefaultValue::Str(value) => Value::Text(value.clone()),
        ModelDefaultValue::Uuid4 => Value::Uuid(Uuid::new_v4()),
        ModelDefaultValue::UtcNow => {
            let now = OffsetDateTime::now_utc();
            let micros = (now.nanosecond() / 1_000) * 1_000;
            Value::Timestamp(now.replace_nanosecond(micros).unwrap_or(now))
        }
    };
    kind_matches(kind?, &value).then_some(value)
}

fn kind_matches(kind: &str, value: &Value) -> bool {
    matches!(
        (kind, value),
        (_, Value::Null)
            | ("str", Value::Text(_))
            | ("int", Value::Int(_))
            | ("float", Value::Float(_))
            | ("bool", Value::Bool(_))
            | ("uuid", Value::Uuid(_))
            | ("datetime", Value::Timestamp(_))
    )
}

fn validated_value(kind: &str, value: Validated) -> Option<Value> {
    let value = match value {
        Validated::Null => Value::Null,
        Validated::Bool(value) => Value::Bool(value),
        Validated::Int(value) => Value::Int(value),
        Validated::Float(value) => Value::Float(value),
        Validated::Str(value) => Value::Text(value),
        Validated::Uuid(value) => Value::Uuid(value),
        Validated::Datetime(value) => Value::Timestamp(value.to_offset(UtcOffset::UTC)),
        Validated::NaiveDatetime(_) | Validated::List(_) | Validated::Object(_) => return None,
    };
    kind_matches(kind, &value).then_some(value)
}

fn write_path_id(plan: &CrudPlan, params: &[(String, String)]) -> Option<Value> {
    let (_, raw) = params.iter().find(|(name, _)| name == "item_id")?;
    let decoded = decode_path_parameter(raw)?;
    match plan.model_field_kind(&plan.primary_key)? {
        "str" => Some(Value::Text(decoded)),
        "int" if strict_integer_path(&decoded) => decoded.parse().ok().map(Value::Int),
        "uuid" if strict_uuid_path(&decoded) => Uuid::parse_str(&decoded).ok().map(Value::Uuid),
        _ => None,
    }
}

fn strict_integer_path(value: &str) -> bool {
    let digits = value.strip_prefix('-').unwrap_or(value);
    !digits.is_empty()
        && digits.len() <= 18
        && digits.is_ascii()
        && digits.bytes().all(|byte| byte.is_ascii_digit())
}

fn strict_uuid_path(value: &str) -> bool {
    value.len() == 36
        && value.bytes().enumerate().all(|(index, byte)| match index {
            8 | 13 | 18 | 23 => byte == b'-',
            _ => byte.is_ascii_hexdigit(),
        })
}

async fn execute_transaction(
    plan: &CrudPlan,
    dialect: &str,
    transaction: Transaction,
    prepared: PreparedWrite,
    body: Option<Vec<u8>>,
    request_id: &str,
    deadline: tokio::time::Instant,
) -> NativeWrite {
    let outcome = tokio::time::timeout_at(
        deadline,
        execute_in_transaction(plan, dialect, &transaction, prepared, request_id),
    )
    .await;
    match outcome {
        Err(_) => {
            let _ = rollback_before_deadline(transaction, deadline).await;
            NativeWrite::Timeout
        }
        Ok(Err(error)) => {
            if rollback_before_deadline(transaction, deadline).await {
                NativeWrite::Failure(error)
            } else {
                NativeWrite::Timeout
            }
        }
        Ok(Ok(TransactionResult::Reply { status, body })) => {
            match tokio::time::timeout_at(deadline, transaction.commit()).await {
                Ok(Ok(())) => NativeWrite::Reply { status, body },
                Ok(Err(error)) => NativeWrite::Failure(error),
                Err(_) => NativeWrite::Timeout,
            }
        }
        Ok(Ok(TransactionResult::NotFound(message))) => {
            if rollback_before_deadline(transaction, deadline).await {
                NativeWrite::NotFound(message)
            } else {
                NativeWrite::Timeout
            }
        }
        Ok(Ok(TransactionResult::Forbidden(body))) => {
            if rollback_before_deadline(transaction, deadline).await {
                NativeWrite::Reply { status: 403, body }
            } else {
                NativeWrite::Timeout
            }
        }
        Ok(Ok(TransactionResult::Replay)) => {
            if rollback_before_deadline(transaction, deadline).await {
                NativeWrite::Defer(body)
            } else {
                NativeWrite::Timeout
            }
        }
    }
}

async fn rollback_before_deadline(
    transaction: Transaction,
    deadline: tokio::time::Instant,
) -> bool {
    matches!(
        tokio::time::timeout_at(deadline, transaction.rollback()).await,
        Ok(Ok(()))
    )
}

async fn execute_in_transaction(
    plan: &CrudPlan,
    dialect: &str,
    transaction: &Transaction,
    prepared: PreparedWrite,
    request_id: &str,
) -> Result<TransactionResult, DbError> {
    match prepared {
        PreparedWrite::Create(values) => {
            create_in_transaction(plan, dialect, transaction, values).await
        }
        PreparedWrite::Update { id, values } => {
            update_in_transaction(plan, dialect, transaction, id, values, request_id).await
        }
        PreparedWrite::Delete(id) => {
            delete_in_transaction(plan, dialect, transaction, id, request_id).await
        }
    }
}

async fn create_in_transaction(
    plan: &CrudPlan,
    dialect: &str,
    transaction: &Transaction,
    supplied: Vec<(String, Value)>,
) -> Result<TransactionResult, DbError> {
    let values = materialize_create(plan, supplied)?;
    let primary_key = plan
        .create_columns()
        .iter()
        .position(|name| name == &plan.primary_key)
        .and_then(|index| values.get(index))
        .cloned()
        .ok_or_else(|| DbError::new(ErrorKind::Other, "CRUD create has no primary key"))?;
    let sql = plan
        .create_sql(dialect)
        .ok_or_else(|| DbError::new(ErrorKind::Other, "CRUD create SQL is unavailable"))?;
    let parameters = values;
    let mut write_rows = if is_mysql(dialect) {
        mysql_create(transaction, plan, sql, &parameters, &primary_key).await?
    } else {
        transaction.fetch(sql, &parameters).await?
    };
    if write_rows.is_empty() {
        return Err(DbError::new(
            ErrorKind::Other,
            "CRUD insert returned no row",
        ));
    }
    write_rows.clear();
    let read_back_sql = plan
        .read_back_sql(dialect)
        .ok_or_else(|| DbError::new(ErrorKind::Other, "CRUD read-back SQL is unavailable"))?;
    let rows = transaction.fetch(read_back_sql, &[primary_key]).await?;
    match rows.as_slice() {
        [] => Ok(TransactionResult::NotFound(format!(
            "{} not found",
            plan.model_name
        ))),
        [row] => match crate::rows::encode_item(row, plan.read_rows()) {
            Some(body) => Ok(TransactionResult::Reply { status: 201, body }),
            None => Ok(TransactionResult::Replay),
        },
        _ => Err(DbError::new(
            ErrorKind::Other,
            "database returned multiple rows for a primary key",
        )),
    }
}

async fn mysql_create(
    transaction: &Transaction,
    plan: &CrudPlan,
    insert_sql: &str,
    parameters: &[Value],
    primary_key: &Value,
) -> Result<Vec<Vec<(String, Value)>>, DbError> {
    let savepoint = required_sql(plan.savepoint_sql("mysql"))?;
    transaction.execute(savepoint, &[]).await?;
    if let Err(error) = transaction.execute(insert_sql, parameters).await {
        rollback_savepoint(transaction, plan).await;
        return Err(error);
    }
    let select = required_sql(plan.write_read_back_sql("mysql"))?;
    let rows = match transaction
        .fetch(select, std::slice::from_ref(primary_key))
        .await
    {
        Ok(rows) => rows,
        Err(error) => {
            rollback_savepoint(transaction, plan).await;
            return Err(error);
        }
    };
    let release = required_sql(plan.release_savepoint_sql("mysql"))?;
    if let Err(error) = transaction.execute(release, &[]).await {
        rollback_savepoint(transaction, plan).await;
        return Err(error);
    }
    Ok(rows)
}

async fn update_in_transaction(
    plan: &CrudPlan,
    dialect: &str,
    transaction: &Transaction,
    id: Value,
    supplied: Vec<(String, Value)>,
    request_id: &str,
) -> Result<TransactionResult, DbError> {
    let visible_sql = required_sql(plan.retrieve_sql(dialect))?;
    let visible = transaction
        .fetch(visible_sql, std::slice::from_ref(&id))
        .await?;
    if visible.is_empty() {
        return Ok(TransactionResult::NotFound(format!(
            "{} not found",
            plan.model_name
        )));
    }
    if visible.len() > 1 {
        return Err(DbError::new(
            ErrorKind::Other,
            "database returned multiple rows for a primary key",
        ));
    }
    let values = add_update_timestamp(plan, supplied);
    if !values.is_empty() {
        let columns = values
            .iter()
            .map(|(name, _)| name.clone())
            .collect::<Vec<_>>();
        let sql = plan
            .update_sql(dialect, &columns)
            .map_err(|_| DbError::new(ErrorKind::Other, "CRUD update SQL is unavailable"))?;
        let mut parameters = values
            .into_iter()
            .map(|(_, value)| value)
            .collect::<Vec<_>>();
        parameters.push(id.clone());
        let written = if is_mysql(dialect) {
            mysql_update(transaction, plan, &sql, &parameters, &id).await?
        } else {
            transaction.fetch(&sql, &parameters).await?
        };
        if written.is_empty() {
            return refused_result(plan, dialect, transaction, id, request_id).await;
        }
    }
    let read_back_sql = required_sql(plan.read_back_sql(dialect))?;
    let rows = transaction
        .fetch(read_back_sql, std::slice::from_ref(&id))
        .await?;
    match rows.as_slice() {
        [] => Ok(TransactionResult::NotFound(format!(
            "{} not found",
            plan.model_name
        ))),
        [row] => match crate::rows::encode_item(row, plan.read_rows()) {
            Some(body) => Ok(TransactionResult::Reply { status: 200, body }),
            None => Ok(TransactionResult::Replay),
        },
        _ => Err(DbError::new(
            ErrorKind::Other,
            "database returned multiple rows for a primary key",
        )),
    }
}

async fn mysql_update(
    transaction: &Transaction,
    plan: &CrudPlan,
    update_sql: &str,
    parameters: &[Value],
    primary_key: &Value,
) -> Result<Vec<Vec<(String, Value)>>, DbError> {
    let savepoint = required_sql(plan.savepoint_sql("mysql"))?;
    transaction.execute(savepoint, &[]).await?;
    if let Err(error) = transaction.execute(update_sql, parameters).await {
        rollback_savepoint(transaction, plan).await;
        return Err(error);
    }
    let select = required_sql(plan.write_read_back_sql("mysql"))?;
    let rows = match transaction
        .fetch(select, std::slice::from_ref(primary_key))
        .await
    {
        Ok(rows) => rows,
        Err(error) => {
            rollback_savepoint(transaction, plan).await;
            return Err(error);
        }
    };
    let release = required_sql(plan.release_savepoint_sql("mysql"))?;
    if let Err(error) = transaction.execute(release, &[]).await {
        rollback_savepoint(transaction, plan).await;
        return Err(error);
    }
    Ok(rows)
}

async fn refused_result(
    plan: &CrudPlan,
    dialect: &str,
    transaction: &Transaction,
    id: Value,
    request_id: &str,
) -> Result<TransactionResult, DbError> {
    let sql = required_sql(plan.retrieve_sql(dialect))?;
    if transaction
        .fetch(sql, std::slice::from_ref(&id))
        .await?
        .is_empty()
    {
        return Ok(TransactionResult::NotFound(format!(
            "{} not found",
            plan.model_name
        )));
    }
    let Some(body) = error_body(
        request_id,
        "forbidden",
        Some("The item could not be changed."),
    ) else {
        return Ok(TransactionResult::Replay);
    };
    Ok(TransactionResult::Forbidden(body))
}

async fn delete_in_transaction(
    plan: &CrudPlan,
    dialect: &str,
    transaction: &Transaction,
    id: Value,
    request_id: &str,
) -> Result<TransactionResult, DbError> {
    let visible_sql = required_sql(plan.retrieve_sql(dialect))?;
    let visible = transaction
        .fetch(visible_sql, std::slice::from_ref(&id))
        .await?;
    if visible.is_empty() {
        return Ok(TransactionResult::NotFound(format!(
            "{} not found",
            plan.model_name
        )));
    }
    let delete_sql = required_sql(plan.delete_sql(dialect))?;
    if transaction
        .execute(delete_sql, std::slice::from_ref(&id))
        .await?
        == 0
    {
        return refused_result(plan, dialect, transaction, id, request_id).await;
    }
    Ok(TransactionResult::Reply {
        status: 204,
        body: Vec::new(),
    })
}

fn materialize_create(
    plan: &CrudPlan,
    supplied: Vec<(String, Value)>,
) -> Result<Vec<Value>, DbError> {
    let mut values = HashMap::with_capacity(plan.create_columns().len());
    values.extend(supplied);
    let mut ordered = Vec::with_capacity(plan.create_columns().len());
    for column in plan.create_columns() {
        if let Some(value) = values.remove(column) {
            ordered.push(value);
            continue;
        }
        let default = plan
            .model_defaults()
            .iter()
            .find(|default| &default.column == column)
            .ok_or_else(|| DbError::new(ErrorKind::Other, "CRUD create default is unavailable"))?;
        let value = default_value(default, plan.model_field_kind(column))
            .ok_or_else(|| DbError::new(ErrorKind::Other, "CRUD create value is unavailable"))?;
        ordered.push(value);
    }
    Ok(ordered)
}

fn add_update_timestamp(plan: &CrudPlan, mut values: Vec<(String, Value)>) -> Vec<(String, Value)> {
    if values.is_empty() {
        return values;
    }
    let Some(column) = &plan.updated_at else {
        return values;
    };
    let now = OffsetDateTime::now_utc();
    let micros = (now.nanosecond() / 1_000) * 1_000;
    let timestamp = Value::Timestamp(now.replace_nanosecond(micros).unwrap_or(now));
    match values.iter_mut().find(|(name, _)| name == column) {
        Some((_, value)) => *value = timestamp,
        None => values.push((column.clone(), timestamp)),
    }
    values
}

async fn rollback_savepoint(transaction: &Transaction, plan: &CrudPlan) {
    if let Some(sql) = plan.rollback_savepoint_sql("mysql") {
        let _ = transaction.execute(sql, &[]).await;
    }
}

fn required_sql(sql: Option<&str>) -> Result<&str, DbError> {
    sql.ok_or_else(|| DbError::new(ErrorKind::Other, "CRUD SQL template is unavailable"))
}

fn is_mysql(dialect: &str) -> bool {
    matches!(dialect, "mysql" | "mariadb")
}

async fn execute_read_with<D: ReadDatabase>(
    plan: &CrudPlan,
    database: &D,
    path_params: &[(String, String)],
    raw_query: &[u8],
) -> NativeRead {
    match plan.action.as_str() {
        "list" => execute_list(plan, database, raw_query).await,
        "retrieve" => execute_retrieve(plan, database, path_params).await,
        _ => NativeRead::Defer,
    }
}

async fn execute_list<D: ReadDatabase>(
    plan: &CrudPlan,
    database: &D,
    raw_query: &[u8],
) -> NativeRead {
    let Some(page) = parse_page(raw_query, plan.page_size, plan.max_page_size) else {
        return NativeRead::Defer;
    };
    let dialect = database.dialect();
    let Some(count_sql) = plan.count_sql(dialect) else {
        return NativeRead::Defer;
    };
    let count_rows = match database.fetch(count_sql, &[]).await {
        Ok(rows) => rows,
        Err(error) => return NativeRead::Failure(error),
    };
    let Some(total) = count_rows
        .first()
        .and_then(|row| row.first())
        .filter(|(name, _)| name == "count")
        .and_then(|(_, value)| match value {
            Value::Int(count) => u64::try_from(*count).ok(),
            _ => None,
        })
    else {
        return NativeRead::Defer;
    };
    let sql = match plan.list_sql(dialect, page.size, page.offset) {
        Ok(sql) => sql,
        Err(_) => return NativeRead::Defer,
    };
    let rows = match database.fetch(&sql, &[]).await {
        Ok(rows) => rows,
        Err(error) => return NativeRead::Failure(error),
    };
    let page_info = (page.number, page.size, total);
    match crate::rows::encode_page(&rows, plan.read_rows(), page.number, page.size, total) {
        Some(body) => NativeRead::Reply { status: 200, body },
        None => NativeRead::Rows(rows, Some(page_info)),
    }
}

async fn execute_retrieve<D: ReadDatabase>(
    plan: &CrudPlan,
    database: &D,
    path_params: &[(String, String)],
) -> NativeRead {
    let Some((_, raw_id)) = path_params.iter().find(|(name, _)| name == "item_id") else {
        return NativeRead::Defer;
    };
    let Some(decoded_id) = crate::request::decode_path_parameter(raw_id) else {
        return NativeRead::Defer;
    };
    let Some(value) = primary_key_value(plan, &decoded_id) else {
        return NativeRead::Defer;
    };
    let dialect = database.dialect();
    let Some(sql) = plan.retrieve_sql(dialect) else {
        return NativeRead::Defer;
    };
    let rows = match database.fetch(sql, &[value]).await {
        Ok(rows) => rows,
        Err(error) => return NativeRead::Failure(error),
    };
    if rows.is_empty() {
        return NativeRead::NotFound;
    }
    if rows.len() != 1 {
        return NativeRead::Defer;
    }
    match crate::rows::encode_item(&rows[0], plan.read_rows()) {
        Some(body) => NativeRead::Reply { status: 200, body },
        None => NativeRead::Rows(rows, None),
    }
}

fn primary_key_value(plan: &CrudPlan, raw: &str) -> Option<Value> {
    match plan
        .model_fields
        .iter()
        .find(|(name, _, _)| name == &plan.primary_key)?
        .1
        .as_str()
    {
        "str" => Some(Value::Text(raw.to_owned())),
        "int" => raw.parse().ok().map(Value::Int),
        "uuid" => uuid::Uuid::parse_str(raw).ok().map(Value::Uuid),
        _ => None,
    }
}

fn parse_page(raw: &[u8], default_size: u64, max_size: u64) -> Option<Page> {
    let pairs = parse_query(raw, 1000).ok()?;
    let mut page = None;
    let mut size = None;
    for (name, value) in pairs {
        match name.as_str() {
            "page" if page.is_none() => page = Some(parse_number(&value, 1_000_000_000)?),
            "page_size" if size.is_none() => size = Some(parse_number(&value, max_size)?),
            "page" | "page_size" => return None,
            _ => {}
        }
    }
    let number = page.unwrap_or(1);
    let size = size.unwrap_or(default_size);
    if size == 0 || size > max_size {
        return None;
    }
    let offset = number.checked_sub(1)?.checked_mul(size)?;
    Some(Page {
        number,
        size,
        offset,
    })
}

fn parse_number(raw: &str, upper: u64) -> Option<u64> {
    if raw.is_empty()
        || raw.len() > 18
        || !raw.is_ascii()
        || !raw.bytes().all(|b| b.is_ascii_digit())
    {
        return None;
    }
    let value = raw.parse::<u64>().ok()?;
    (value > 0 && value <= upper).then_some(value)
}

#[cfg(test)]
mod tests {
    use std::sync::Mutex;

    use hinoki_orm::Value;

    use super::{execute_read_with, parse_page, FetchFuture, NativeRead, ReadDatabase};
    use crate::crudplan::CrudPlan;

    struct StubDatabase {
        statements: Mutex<Vec<(String, Vec<Value>)>>,
    }

    impl ReadDatabase for StubDatabase {
        fn dialect(&self) -> &str {
            "postgres"
        }

        fn fetch<'a>(&'a self, sql: &'a str, values: &'a [Value]) -> FetchFuture<'a> {
            Box::pin(async move {
                self.statements
                    .lock()
                    .unwrap()
                    .push((sql.to_owned(), values.to_vec()));
                if sql.to_ascii_lowercase().contains("count(") {
                    Ok(vec![vec![("count".to_owned(), Value::Int(3))]])
                } else {
                    let id = values.first().cloned().unwrap_or(Value::Int(2));
                    Ok(vec![vec![
                        ("id".to_owned(), id),
                        ("name".to_owned(), Value::Text("second".to_owned())),
                    ]])
                }
            })
        }
    }

    fn plan(action: &str) -> CrudPlan {
        CrudPlan::new(
            action.to_owned(),
            "Item".to_owned(),
            "items".to_owned(),
            vec!["id".to_owned(), "name".to_owned()],
            "id".to_owned(),
            Vec::new(),
            20,
            100,
            vec![
                ("id".to_owned(), "int".to_owned(), false),
                ("name".to_owned(), "str".to_owned(), false),
            ],
            Vec::new(),
            Vec::new(),
            Vec::new(),
            Vec::new(),
            Vec::new(),
            None,
            vec![
                ("id".to_owned(), "id".to_owned(), "int".to_owned(), false),
                ("name".to_owned(), "name".to_owned(), "str".to_owned(), false),
            ],
            br#"{"error":"validation_error","request_id":"0123456789abcdeffedcba9876543210","details":[{"path":"body","code":"invalid","message":"invalid"}]}"#.to_vec(),
            None,
            None,
        )
        .unwrap()
    }

    #[test]
    fn parses_bounded_page_number_parameters() {
        assert_eq!(
            parse_page(b"page=3&page_size=7", 20, 100).map(|page| (
                page.number,
                page.size,
                page.offset
            )),
            Some((3, 7, 14))
        );
        assert_eq!(
            parse_page(b"filter=x", 20, 100).map(|page| (page.number, page.size, page.offset)),
            Some((1, 20, 0))
        );
    }

    #[test]
    fn leaves_invalid_or_ambiguous_pages_for_python_validation() {
        for query in [
            b"page=0".as_slice(),
            b"page_size=101",
            b"page=1&page=2",
            b"page=1&page_size=2&page_size=3",
            b"page=18446744073709551616",
            b"page=+2",
            b"page=%FF",
        ] {
            assert!(parse_page(query, 20, 100).is_none(), "{query:?}");
        }
    }

    #[tokio::test]
    async fn list_counts_before_compiling_and_fetching_the_requested_window() {
        let database = StubDatabase {
            statements: Mutex::new(Vec::new()),
        };
        let result = execute_read_with(&plan("list"), &database, &[], b"page=2&page_size=1").await;
        let NativeRead::Reply { status, body } = result else {
            panic!("valid native list did not encode");
        };
        assert_eq!(status, 200);
        assert_eq!(
            body,
            br#"{"items":[{"id":2,"name":"second"}],"page":2,"page_size":1,"total":3,"pages":3}"#
        );
        let statements = database.statements.lock().unwrap();
        assert_eq!(statements.len(), 2);
        assert!(statements[0].0.to_ascii_lowercase().contains("count("));
        assert!(statements[1].0.contains("LIMIT 1"));
        assert!(statements[1].0.contains("OFFSET 1"));
    }

    #[tokio::test]
    async fn retrieve_uses_decoded_path_id_and_returns_one_projected_item() {
        let database = StubDatabase {
            statements: Mutex::new(Vec::new()),
        };
        let params = vec![("item_id".to_owned(), "%37".to_owned())];
        let result = execute_read_with(&plan("retrieve"), &database, &params, b"").await;
        let NativeRead::Reply { status, body } = result else {
            panic!("valid native retrieve did not encode");
        };
        assert_eq!(status, 200);
        assert_eq!(body, br#"{"id":7,"name":"second"}"#);
        let statements = database.statements.lock().unwrap();
        assert_eq!(statements.len(), 1);
        assert!(statements[0].0.contains("LIMIT 2"));
        assert_eq!(statements[0].1, [Value::Int(7)]);
    }
}
