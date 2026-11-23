//! GIL-free execution for eligible generated CRUD routes.

use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::time::Duration;

use hinoki_orm::query::{like_pattern, Filter, Op};
use hinoki_orm::{Database, DbError, ErrorKind, Transaction, Value};
use time::{OffsetDateTime, UtcOffset};
use uuid::Uuid;

use crate::crudplan::{CrudPlan, ModelDefault, ModelDefaultValue};
use crate::filters::Typed;
use crate::nativelinks::{self, InTransaction, LinkWork, Refused};
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
    Unprocessable(Vec<u8>),
    Replay,
}

/// The owner column and the caller's id every update and delete must match.
struct OwnerScope {
    column: String,
    value: String,
}

impl OwnerScope {
    fn owns(&self, row: &[(String, Value)]) -> bool {
        row.iter().any(|(name, value)| {
            name == &self.column && matches!(value, Value::Text(text) if text == &self.value)
        })
    }
}

enum PreparedWrite {
    Create(Vec<(String, Value)>, LinkWork),
    Update {
        id: Value,
        values: Vec<(String, Value)>,
        owner: Option<OwnerScope>,
        links: LinkWork,
    },
    Delete {
        id: Value,
        owner: Option<OwnerScope>,
    },
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

pub(crate) type FetchFuture<'a> =
    Pin<Box<dyn Future<Output = Result<Vec<Vec<(String, Value)>>, DbError>> + Send + 'a>>;

pub(crate) trait ReadDatabase {
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
    caller: Option<&str>,
) -> NativeRead {
    execute_read_with(plan, database, path_params, raw_query, caller).await
}

#[allow(clippy::too_many_arguments)]
pub(crate) async fn execute_write(
    plan: &CrudPlan,
    database: &Database,
    request: &PreparedRequest,
    path_params: &[(String, String)],
    raw_query: &[u8],
    max_body_bytes: usize,
    timeout: Duration,
    caller: Option<&str>,
) -> NativeWrite {
    let owner = match (plan.owner.as_deref(), caller) {
        (None, _) => None,
        (Some(column), Some(value)) => Some(OwnerScope {
            column: column.to_owned(),
            value: value.to_owned(),
        }),
        (Some(_), None) => return NativeWrite::Defer(None),
    };
    let deadline = tokio::time::Instant::now() + timeout;
    let body = match read_write_body(plan, request, max_body_bytes, deadline).await {
        Ok(body) => body,
        Err(NativeWrite::Timeout) => return http_error(request, 504, "timeout", None),
        Err(result) => return result,
    };
    let prepared = match prepare_write(
        plan,
        request,
        path_params,
        raw_query,
        body.as_deref(),
        (owner, caller),
    ) {
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
        Ok(Err(kiso::BodyError::TooLarge)) => {
            Err(http_error(request, 413, "payload_too_large", None))
        }
        Ok(Err(kiso::BodyError::Timeout)) => Err(http_error(request, 408, "request_timeout", None)),
        Ok(Err(kiso::BodyError::Malformed)) => Err(http_error(
            request,
            400,
            "disconnected",
            Some("the request body is malformed"),
        )),
        Ok(Err(kiso::BodyError::Disconnected)) => Err(http_error(
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
    (owner, caller): (Option<OwnerScope>, Option<&str>),
) -> Result<PreparedWrite, NativeWrite> {
    if parse_query(raw_query, 1000).is_err() {
        return Err(http_error(
            request,
            400,
            "invalid_query",
            Some("malformed query string"),
        ));
    }
    let caller = caller.map(str::to_owned);
    match plan.action.as_str() {
        "create" => prepare_create(
            plan,
            request,
            body.ok_or(NativeWrite::Defer(None))?,
            (owner, caller),
        ),
        "update" => prepare_update(
            plan,
            request,
            path_params,
            body.ok_or(NativeWrite::Defer(None))?,
            (owner, caller),
        ),
        "delete" => {
            let id = write_path_id(plan, path_params).ok_or(NativeWrite::Defer(None))?;
            Ok(PreparedWrite::Delete { id, owner })
        }
        _ => Err(NativeWrite::Defer(body.map(<[u8]>::to_vec))),
    }
}

fn prepare_create(
    plan: &CrudPlan,
    request: &PreparedRequest,
    body: &[u8],
    (owner, caller): (Option<OwnerScope>, Option<String>),
) -> Result<PreparedWrite, NativeWrite> {
    let json = decode_body(request, body)?;
    let schema = plan
        .create_schema()
        .ok_or(NativeWrite::Defer(Some(body.to_vec())))?;
    let parsed = validate_body(plan, schema, &json, request)?;
    let Some((mut values, sets)) = parsed else {
        return Err(NativeWrite::Defer(Some(body.to_vec())));
    };
    if !append_defaults(&mut values, plan.create_defaults(), plan) {
        return Err(NativeWrite::Defer(Some(body.to_vec())));
    }
    if let Some(owner) = owner {
        // The schema cannot carry the owner column, so this never overrides input.
        values.push((owner.column, Value::Text(owner.value)));
    }
    order_schema_values(&mut values, schema);
    ensure_create_requirements(plan, &values, body)?;
    Ok(PreparedWrite::Create(values, LinkWork { sets, caller }))
}

fn prepare_update(
    plan: &CrudPlan,
    request: &PreparedRequest,
    path_params: &[(String, String)],
    body: &[u8],
    (owner, caller): (Option<OwnerScope>, Option<String>),
) -> Result<PreparedWrite, NativeWrite> {
    let id = write_path_id(plan, path_params).ok_or(NativeWrite::Defer(Some(body.to_vec())))?;
    let json = decode_body(request, body)?;
    let schema = plan
        .update_schema()
        .ok_or(NativeWrite::Defer(Some(body.to_vec())))?;
    let Some((mut values, sets)) = validate_body(plan, schema, &json, request)? else {
        return Err(NativeWrite::Defer(Some(body.to_vec())));
    };
    if !append_defaults(&mut values, plan.update_defaults(), plan) {
        return Err(NativeWrite::Defer(Some(body.to_vec())));
    }
    order_schema_values(&mut values, schema);
    Ok(PreparedWrite::Update {
        id,
        values,
        owner,
        links: LinkWork { sets, caller },
    })
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

type ValidatedBody = (
    Vec<(String, Value)>,
    Vec<(usize, Vec<hinoki_orm::links::Canon>)>,
);

fn validate_body(
    plan: &CrudPlan,
    schema: &NativeSchema,
    json: &Json,
    request: &PreparedRequest,
) -> Result<Option<ValidatedBody>, NativeWrite> {
    let options = Options {
        construct: false,
        use_aliases: true,
        allow_naive_datetime: false,
        skip_fields: std::collections::HashSet::new(),
        input_bounds: true,
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
    let mut sets = Vec::new();
    for (name, value) in fields {
        if let Some(index) = nativelinks::link_index(plan, &name) {
            let Some(keys) = nativelinks::keys_of(&plan.links[index], value) else {
                return Ok(None);
            };
            sets.push((index, keys));
            continue;
        }
        let Some(kind) = plan.model_field_kind(&name) else {
            return Ok(None);
        };
        let Some(value) = validated_value(kind, value) else {
            return Ok(None);
        };
        values.push((name, value));
    }
    Ok(Some((values, sets)))
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

/// Adds each schema default the body left out. A default whose value cannot
/// be stored in its column is not skipped: Python decides what to store.
fn append_defaults(
    values: &mut Vec<(String, Value)>,
    defaults: &[ModelDefault],
    plan: &CrudPlan,
) -> bool {
    for default in defaults {
        if values.iter().any(|(name, _)| name == &default.column) {
            continue;
        }
        let Some(value) = default_value(default, plan.model_field_kind(&default.column)) else {
            return false;
        };
        values.push((default.column.clone(), value));
    }
    true
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
            | ("bytes", Value::Bytes(_))
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
        Validated::Bytes(value) => Value::Bytes(value),
        Validated::NaiveDatetime(_)
        | Validated::Date(_)
        | Validated::Time(_)
        | Validated::Duration(_)
        | Validated::Decimal(_)
        | Validated::Net(..)
        | Validated::List(_)
        | Validated::Object(_) => return None,
    };
    kind_matches(kind, &value).then_some(value)
}

fn write_path_id(plan: &CrudPlan, params: &[(String, String)]) -> Option<Value> {
    let (_, raw) = params.iter().find(|(name, _)| name == "item_id")?;
    let decoded = decode_path_parameter(raw)?;
    primary_key_value(plan, &decoded)
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
        Ok(Ok(refused)) => {
            let answer = refusal(refused, body);
            if rollback_before_deadline(transaction, deadline).await {
                answer
            } else {
                NativeWrite::Timeout
            }
        }
    }
}

/// What the client gets once a transaction that did not succeed is rolled back.
fn refusal(result: TransactionResult, body: Option<Vec<u8>>) -> NativeWrite {
    match result {
        TransactionResult::NotFound(message) => NativeWrite::NotFound(message),
        TransactionResult::Forbidden(body) => NativeWrite::Reply { status: 403, body },
        TransactionResult::Unprocessable(body) => NativeWrite::Reply { status: 422, body },
        TransactionResult::Replay => NativeWrite::Defer(body),
        TransactionResult::Reply { status, body } => NativeWrite::Reply { status, body },
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
        PreparedWrite::Create(values, links) => {
            create_in_transaction(plan, dialect, transaction, values, &links, request_id).await
        }
        PreparedWrite::Update {
            id,
            values,
            owner,
            links,
        } => {
            update_in_transaction(
                plan,
                dialect,
                transaction,
                (id, owner.as_ref()),
                (values, &links),
                request_id,
            )
            .await
        }
        PreparedWrite::Delete { id, owner } => {
            delete_in_transaction(plan, dialect, transaction, (id, owner.as_ref()), request_id)
                .await
        }
    }
}

async fn create_in_transaction(
    plan: &CrudPlan,
    dialect: &str,
    transaction: &Transaction,
    supplied: Vec<(String, Value)>,
    links: &LinkWork,
    request_id: &str,
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
    if let Some(refused) = nativelinks::write_links(
        plan,
        dialect,
        transaction,
        &primary_key,
        (links, hinoki_orm::links::Scope::default()),
    )
    .await?
    {
        return refused_links(refused, request_id);
    }
    let read_back_sql = plan
        .read_back_sql(dialect)
        .ok_or_else(|| DbError::new(ErrorKind::Other, "CRUD read-back SQL is unavailable"))?;
    let rows = transaction.fetch(read_back_sql, &[primary_key]).await?;
    match rows.as_slice() {
        [] => Ok(TransactionResult::NotFound(format!(
            "{} not found",
            plan.model_name
        ))),
        [row] => written_item(plan, (dialect, transaction), row, (201, links)).await,
        _ => Err(DbError::new(
            ErrorKind::Other,
            "database returned multiple rows for a primary key",
        )),
    }
}

/// The written row with the ids its transaction left, or a replay when the
/// row cannot be encoded natively.
async fn written_item(
    plan: &CrudPlan,
    (dialect, transaction): (&str, &Transaction),
    row: &[(String, Value)],
    (status, links): (u16, &LinkWork),
) -> Result<TransactionResult, DbError> {
    let database = InTransaction {
        transaction,
        dialect,
    };
    let Some(ids) = nativelinks::read_ids(
        plan,
        &database,
        std::slice::from_ref(&row.to_vec()),
        links.caller.as_deref(),
    )
    .await?
    else {
        return Ok(TransactionResult::Replay);
    };
    Ok(
        match crate::rows::encode_item_with(row, plan.read_rows(), &ids) {
            Some(body) => TransactionResult::Reply { status, body },
            None => TransactionResult::Replay,
        },
    )
}

fn refused_links(refused: Refused, request_id: &str) -> Result<TransactionResult, DbError> {
    match refused {
        Refused::OwnerMissing | Refused::OwnerGone => {
            forbidden_result(request_id, "You cannot change this item.")
        }
        Refused::Replay => Ok(TransactionResult::Replay),
        Refused::Unknown(field) => Ok(serde_json::to_vec(&ValidationBody {
            error: "validation_error",
            request_id: request_id.to_owned(),
            details: vec![ValidationDetail {
                path: field,
                code: "unknown_reference",
                message: "contains ids that do not exist".to_owned(),
            }],
        })
        .map_or(TransactionResult::Replay, TransactionResult::Unprocessable)),
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
    (id, owner): (Value, Option<&OwnerScope>),
    (supplied, links): (Vec<(String, Value)>, &LinkWork),
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
    if let Some(scope) = owner {
        if !scope.owns(&visible[0]) {
            return forbidden_result(request_id, "You cannot change this item.");
        }
    }
    let values = add_update_timestamp(plan, supplied);
    if !values.is_empty() {
        let written = write_columns(plan, (dialect, transaction), (&id, owner), values).await?;
        if written.is_empty() {
            return refused_result(plan, dialect, transaction, (id, owner), request_id).await;
        }
    }
    let owner_scope = owner.map_or_else(hinoki_orm::links::Scope::default, |scope| {
        hinoki_orm::links::Scope::column_equals(&scope.column, Value::Text(scope.value.clone()))
    });
    if let Some(refused) =
        nativelinks::write_links(plan, dialect, transaction, &id, (links, owner_scope)).await?
    {
        if matches!(refused, Refused::OwnerGone) {
            return Ok(TransactionResult::NotFound(format!(
                "{} not found",
                plan.model_name
            )));
        }
        if matches!(refused, Refused::OwnerMissing) {
            return refused_result(plan, dialect, transaction, (id, owner), request_id).await;
        }
        return refused_links(refused, request_id);
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
        [row] => written_item(plan, (dialect, transaction), row, (200, links)).await,
        _ => Err(DbError::new(
            ErrorKind::Other,
            "database returned multiple rows for a primary key",
        )),
    }
}

/// The UPDATE of the changed columns; no rows come back when it matched none.
async fn write_columns(
    plan: &CrudPlan,
    (dialect, transaction): (&str, &Transaction),
    (id, owner): (&Value, Option<&OwnerScope>),
    values: Vec<(String, Value)>,
) -> Result<Vec<Vec<(String, Value)>>, DbError> {
    let columns = values
        .iter()
        .map(|(name, _)| name.clone())
        .collect::<Vec<_>>();
    let sql = plan
        .update_sql(dialect, &columns)
        .map_err(|_| DbError::new(ErrorKind::Other, "CRUD update SQL is unavailable"))?;
    let parameters = update_parameters(values, id, owner);
    if is_mysql(dialect) {
        let scope = scope_parameters(id, owner);
        mysql_update(transaction, plan, &sql, (&parameters, &scope)).await
    } else {
        transaction.fetch(&sql, &parameters).await
    }
}

fn update_parameters(
    values: Vec<(String, Value)>,
    id: &Value,
    owner: Option<&OwnerScope>,
) -> Vec<Value> {
    let mut parameters = values
        .into_iter()
        .map(|(_, value)| value)
        .collect::<Vec<_>>();
    parameters.push(id.clone());
    if let Some(scope) = owner {
        parameters.push(Value::Text(scope.value.clone()));
    }
    parameters
}

fn scope_parameters(id: &Value, owner: Option<&OwnerScope>) -> Vec<Value> {
    let mut parameters = vec![id.clone()];
    if let Some(scope) = owner {
        parameters.push(Value::Text(scope.value.clone()));
    }
    parameters
}

/// MySQL has no RETURNING: the row is read back with the update's own key and
/// owner filter, so a write that matched no row reads back as empty.
async fn mysql_update(
    transaction: &Transaction,
    plan: &CrudPlan,
    update_sql: &str,
    (parameters, scope): (&[Value], &[Value]),
) -> Result<Vec<Vec<(String, Value)>>, DbError> {
    let savepoint = required_sql(plan.savepoint_sql("mysql"))?;
    transaction.execute(savepoint, &[]).await?;
    if let Err(error) = transaction.execute(update_sql, parameters).await {
        rollback_savepoint(transaction, plan).await;
        return Err(error);
    }
    let select = required_sql(plan.update_read_back_sql("mysql"))?;
    let rows = match transaction.fetch(select, scope).await {
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
    (id, owner): (Value, Option<&OwnerScope>),
    request_id: &str,
) -> Result<TransactionResult, DbError> {
    let sql = required_sql(plan.retrieve_sql(dialect))?;
    let rows = transaction.fetch(sql, std::slice::from_ref(&id)).await?;
    let Some(row) = rows.first() else {
        return Ok(TransactionResult::NotFound(format!(
            "{} not found",
            plan.model_name
        )));
    };
    if owner.is_some_and(|scope| !scope.owns(row)) {
        return forbidden_result(request_id, "You cannot change this item.");
    }
    forbidden_result(request_id, "The item could not be changed.")
}

fn forbidden_result(request_id: &str, message: &str) -> Result<TransactionResult, DbError> {
    Ok(error_body(request_id, "forbidden", Some(message))
        .map_or(TransactionResult::Replay, TransactionResult::Forbidden))
}

async fn delete_in_transaction(
    plan: &CrudPlan,
    dialect: &str,
    transaction: &Transaction,
    (id, owner): (Value, Option<&OwnerScope>),
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
    if let Some(scope) = owner {
        if !scope.owns(&visible[0]) {
            return forbidden_result(request_id, "You cannot change this item.");
        }
    }
    let delete_sql = required_sql(plan.delete_sql(dialect))?;
    let mut parameters = vec![id.clone()];
    if let Some(scope) = owner {
        parameters.push(Value::Text(scope.value.clone()));
    }
    if transaction.execute(delete_sql, &parameters).await? == 0 {
        return refused_result(plan, dialect, transaction, (id, owner), request_id).await;
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
    caller: Option<&str>,
) -> NativeRead {
    match plan.action.as_str() {
        "list" => execute_list(plan, database, raw_query, caller).await,
        "retrieve" => execute_retrieve(plan, database, path_params, raw_query, caller).await,
        _ => NativeRead::Defer,
    }
}

async fn execute_list<D: ReadDatabase>(
    plan: &CrudPlan,
    database: &D,
    raw_query: &[u8],
    caller: Option<&str>,
) -> NativeRead {
    let Some(page) = parse_page(raw_query, plan.page_size, plan.max_page_size) else {
        return NativeRead::Defer;
    };
    let dialect = database.dialect();
    let Some(filtered) = parse_list_filters(plan, dialect, raw_query) else {
        return NativeRead::Defer;
    };
    let Some((count_sql, count_params, list_sql, list_params)) =
        list_statements(plan, dialect, filtered, page)
    else {
        return NativeRead::Defer;
    };
    let count_rows = match database.fetch(&count_sql, &count_params).await {
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
    let rows = match database.fetch(&list_sql, &list_params).await {
        Ok(rows) => rows,
        Err(error) => return NativeRead::Failure(error),
    };
    let page_info = (page.number, page.size, total);
    let ids = match nativelinks::read_ids(plan, database, &rows, caller).await {
        Ok(Some(ids)) => ids,
        Ok(None) => return NativeRead::Defer,
        Err(error) => return NativeRead::Failure(error),
    };
    let wire = (page.number, page.size, total);
    match crate::rows::encode_page_with(&rows, plan.read_rows(), &ids, wire) {
        Some(body) => NativeRead::Reply { status: 200, body },
        None if plan.links.is_empty() => NativeRead::Rows(rows, Some(page_info)),
        None => NativeRead::Defer,
    }
}

type ListQuery = (Vec<Filter<Value>>, Vec<(String, bool)>);
type ListStatements = (String, Vec<Value>, String, Vec<Value>);

fn list_statements(
    plan: &CrudPlan,
    dialect: &str,
    (filters, order): ListQuery,
    page: Page,
) -> Option<ListStatements> {
    if filters.is_empty() && order.is_empty() {
        let count_sql = plan.count_sql(dialect)?.to_owned();
        let list_sql = plan.list_sql(dialect, page.size, page.offset).ok()?;
        return Some((count_sql, Vec::new(), list_sql, Vec::new()));
    }
    let (count_sql, count_params) = plan.filtered_count_sql(dialect, filters.clone()).ok()?;
    let (list_sql, list_params) = plan
        .filtered_list_sql(dialect, filters, order, page.size, page.offset)
        .ok()?;
    Some((count_sql, count_params, list_sql, list_params))
}

fn parse_list_filters(plan: &CrudPlan, dialect: &str, raw_query: &[u8]) -> Option<ListQuery> {
    let Some(spec) = &plan.filters else {
        return Some((Vec::new(), Vec::new()));
    };
    let grouped = group_query_pairs(parse_query(raw_query, 1000).ok()?);
    let reserved = ["page".to_owned(), "page_size".to_owned()];
    let parsed = spec.parse(&grouped, &reserved).ok()?;
    let filters = compile_filter_conditions(plan, dialect, spec.conditions(&parsed))?;
    let order = compile_filter_order(plan, dialect, parsed.order.as_deref())?;
    Some((filters, order))
}

fn group_query_pairs(pairs: Vec<(String, String)>) -> Vec<(String, Vec<String>)> {
    let mut grouped: Vec<(String, Vec<String>)> = Vec::new();
    for (name, value) in pairs {
        if let Some((_, values)) = grouped.iter_mut().find(|(key, _)| key == &name) {
            values.push(value);
        } else {
            grouped.push((name, vec![value]));
        }
    }
    grouped
}

fn compile_filter_conditions(
    plan: &CrudPlan,
    dialect: &str,
    conditions: Vec<(String, &'static str, Vec<Typed>)>,
) -> Option<Vec<Filter<Value>>> {
    let mut out = Vec::with_capacity(conditions.len());
    for (column, op_name, typed_values) in conditions {
        if dialect == "sqlite" && touches_decimal(plan, &column) {
            return None;
        }
        let op = Op::parse(op_name)?;
        let values: Vec<Value> = typed_values.iter().map(Typed::to_orm_value).collect();
        let (op, values) = prebuild_pattern_value(dialect, op, values);
        out.push(Filter { column, op, values });
    }
    Some(out)
}

fn compile_filter_order(
    plan: &CrudPlan,
    dialect: &str,
    requested: Option<&[String]>,
) -> Option<Vec<(String, bool)>> {
    let Some(items) = requested else {
        return Some(Vec::new());
    };
    let mut order = Vec::with_capacity(items.len() + 1);
    for item in items {
        let (column, desc) = match item.strip_prefix('-') {
            Some(name) => (name, true),
            None => (item.as_str(), false),
        };
        if dialect == "sqlite" && plan.model_field_kind(column) == Some("decimal") {
            return None;
        }
        order.push((column.to_owned(), desc));
    }
    if !order.iter().any(|(column, _)| column == &plan.primary_key) {
        order.push((plan.primary_key.clone(), false));
    }
    Some(order)
}

fn touches_decimal(plan: &CrudPlan, column: &str) -> bool {
    column
        .split(',')
        .any(|name| plan.model_field_kind(name) == Some("decimal"))
}

fn prebuild_pattern_value(dialect: &str, op: Op, values: Vec<Value>) -> (Op, Vec<Value>) {
    let pattern_op = match op {
        Op::Contains => Op::ContainsPattern,
        Op::ContainsAny => Op::ContainsAnyPattern,
        _ => return (op, values),
    };
    if is_mysql(dialect) {
        return (op, values);
    }
    match values.as_slice() {
        [Value::Text(text)] => (pattern_op, vec![Value::Text(like_pattern(text))]),
        _ => (op, values),
    }
}

async fn execute_retrieve<D: ReadDatabase>(
    plan: &CrudPlan,
    database: &D,
    path_params: &[(String, String)],
    raw_query: &[u8],
    caller: Option<&str>,
) -> NativeRead {
    if parse_query(raw_query, 1000).is_err() {
        return NativeRead::Defer;
    }
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
    let ids = match nativelinks::read_ids(plan, database, &rows, caller).await {
        Ok(Some(ids)) => ids,
        Ok(None) => return NativeRead::Defer,
        Err(error) => return NativeRead::Failure(error),
    };
    match crate::rows::encode_item_with(&rows[0], plan.read_rows(), &ids) {
        Some(body) => NativeRead::Reply { status: 200, body },
        None if plan.links.is_empty() => NativeRead::Rows(rows, None),
        None => NativeRead::Defer,
    }
}

/// The key exactly as Python's path conversion reads it. Any other spelling
/// (a sign, a braced or unhyphenated UUID, more digits than fit) is left to
/// Python, which answers it.
fn primary_key_value(plan: &CrudPlan, raw: &str) -> Option<Value> {
    match plan.model_field_kind(&plan.primary_key)? {
        "str" => Some(Value::Text(raw.to_owned())),
        "int" if strict_integer_path(raw) => raw.parse().ok().map(Value::Int),
        "uuid" if strict_uuid_path(raw) => Uuid::parse_str(raw).ok().map(Value::Uuid),
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

    use super::{
        append_defaults, execute_read_with, parse_page, primary_key_value, FetchFuture, NativeRead,
        ReadDatabase,
    };
    use crate::crudplan::{CrudPlan, ModelDefault, ModelDefaultValue};

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
            None,
        )
        .unwrap()
    }

    #[test]
    fn only_a_text_row_value_equal_to_the_caller_is_owned() {
        let scope = super::OwnerScope {
            column: "owner".to_owned(),
            value: "u1".to_owned(),
        };
        let row = |value: Value| vec![("owner".to_owned(), value)];
        assert!(scope.owns(&row(Value::Text("u1".to_owned()))));
        assert!(!scope.owns(&row(Value::Text("u2".to_owned()))));
        assert!(!scope.owns(&row(Value::Text(String::new()))));
        assert!(!scope.owns(&row(Value::Null)));
        assert!(!scope.owns(&[("other".to_owned(), Value::Text("u1".to_owned()))]));
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
        let result =
            execute_read_with(&plan("list"), &database, &[], b"page=2&page_size=1", None).await;
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
        let result = execute_read_with(&plan("retrieve"), &database, &params, b"", None).await;
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

    #[test]
    fn integer_keys_are_taken_only_in_the_spelling_python_accepts() {
        let plan = plan("retrieve");
        for raw in ["5", "-5", "0005", "999999999999999999"] {
            assert!(primary_key_value(&plan, raw).is_some(), "{raw}");
        }
        for raw in [
            "+5",
            "",
            "-",
            "1_0",
            "5.0",
            "\u{663}",
            "1000000000000000000",
        ] {
            assert!(primary_key_value(&plan, raw).is_none(), "{raw}");
        }
    }

    #[test]
    fn uuid_keys_are_taken_only_in_the_canonical_hyphenated_spelling() {
        let mut plan = plan("retrieve");
        plan.model_fields[0].1 = "uuid".to_owned();
        let canonical = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d";
        assert!(primary_key_value(&plan, canonical).is_some());
        assert!(primary_key_value(&plan, &canonical.to_uppercase()).is_some());
        for raw in [
            "0a1b2c3d4e5f4a6b8c7d9e0f1a2b3c4d",
            "{0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d}",
            "urn:uuid:0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
        ] {
            assert!(primary_key_value(&plan, raw).is_none(), "{raw}");
        }
    }

    #[tokio::test]
    async fn retrieve_defers_when_the_query_string_is_malformed() {
        let database = StubDatabase {
            statements: Mutex::new(Vec::new()),
        };
        let params = vec![("item_id".to_owned(), "7".to_owned())];
        for query in [b"%FF".as_slice(), b"a=%FF", b"%C3"] {
            let result =
                execute_read_with(&plan("retrieve"), &database, &params, query, None).await;
            assert!(matches!(result, NativeRead::Defer), "{query:?}");
        }
        assert!(database.statements.lock().unwrap().is_empty());
        let valid = execute_read_with(&plan("retrieve"), &database, &params, b"a=1", None).await;
        assert!(matches!(valid, NativeRead::Reply { .. }));
    }

    #[test]
    fn list_sql_for_a_window_is_the_compiled_statement() {
        let plan = plan("list");
        for (dialect, quote) in [("postgres", '"'), ("sqlite", '"'), ("mysql", '`')] {
            let q = |name: &str| format!("{quote}{name}{quote}");
            let expected = format!(
                "SELECT {}, {} FROM {} ORDER BY {} ASC LIMIT 7 OFFSET 14",
                q("id"),
                q("name"),
                q("items"),
                q("id")
            );
            assert_eq!(plan.list_sql(dialect, 7, 14).unwrap(), expected);
        }
    }

    #[test]
    fn a_default_that_does_not_fit_its_column_is_never_skipped() {
        let plan = plan("create");
        let default = |column: &str, value| ModelDefault {
            column: column.to_owned(),
            value,
        };
        let mut values = Vec::new();
        assert!(append_defaults(
            &mut values,
            &[default("name", ModelDefaultValue::Str("n".to_owned()))],
            &plan
        ));
        assert_eq!(values, [("name".to_owned(), Value::Text("n".to_owned()))]);
        for (column, value) in [
            ("name", ModelDefaultValue::Int(1)),
            ("id", ModelDefaultValue::Float(1.0)),
            ("id", ModelDefaultValue::Bool(true)),
            ("missing", ModelDefaultValue::Int(1)),
            ("id", ModelDefaultValue::Required),
        ] {
            let mut values = Vec::new();
            assert!(!append_defaults(
                &mut values,
                &[default(column, value)],
                &plan
            ));
            assert!(values.is_empty());
        }
    }

    fn filtered_plan(dialect_decimal: bool) -> CrudPlan {
        use std::sync::Arc;

        use crate::filters::{Field, Kind, Limits, Shape, Spec};

        let mut p = CrudPlan::new(
            "list".to_owned(),
            "Item".to_owned(),
            "items".to_owned(),
            vec![
                "id".to_owned(),
                "name".to_owned(),
                "price".to_owned(),
                "note".to_owned(),
            ],
            "id".to_owned(),
            Vec::new(),
            20,
            100,
            vec![
                ("id".to_owned(), "int".to_owned(), false),
                ("name".to_owned(), "str".to_owned(), false),
                ("price".to_owned(), "int".to_owned(), false),
                ("note".to_owned(), "str".to_owned(), true),
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
            None,
        )
        .unwrap();
        if dialect_decimal {
            p.model_fields[2].1 = "decimal".to_owned();
        }
        let spec = Spec::new(
            vec![
                Field {
                    name: "name".to_owned(),
                    kind: Kind::Str,
                    shape: Shape::In,
                    nullable: false,
                    choices: Vec::new(),
                },
                Field {
                    name: "price".to_owned(),
                    kind: if dialect_decimal {
                        Kind::Decimal
                    } else {
                        Kind::Int
                    },
                    shape: Shape::Range,
                    nullable: false,
                    choices: Vec::new(),
                },
                Field {
                    name: "note".to_owned(),
                    kind: Kind::Str,
                    shape: Shape::Eq,
                    nullable: true,
                    choices: Vec::new(),
                },
            ],
            vec!["name".to_owned(), "note".to_owned()],
            vec!["price".to_owned(), "name".to_owned()],
            Limits {
                max_in: 50,
                max_text: 256,
                max_search: 128,
            },
        )
        .unwrap();
        p.attach_filters(Some(Arc::new(spec))).unwrap();
        p
    }

    struct DialectStub {
        dialect: &'static str,
        statements: Mutex<Vec<(String, Vec<Value>)>>,
    }

    impl ReadDatabase for DialectStub {
        fn dialect(&self) -> &str {
            self.dialect
        }

        fn fetch<'a>(&'a self, sql: &'a str, values: &'a [Value]) -> FetchFuture<'a> {
            Box::pin(async move {
                self.statements
                    .lock()
                    .unwrap()
                    .push((sql.to_owned(), values.to_vec()));
                if sql.to_ascii_lowercase().contains("count(") {
                    Ok(vec![vec![("count".to_owned(), Value::Int(1))]])
                } else {
                    Ok(vec![vec![
                        ("id".to_owned(), Value::Int(4)),
                        ("name".to_owned(), Value::Text("alpha".to_owned())),
                        ("price".to_owned(), Value::Int(15)),
                        ("note".to_owned(), Value::Null),
                    ]])
                }
            })
        }
    }

    #[tokio::test]
    async fn filtered_list_compiles_predicates_search_and_stable_ordering() {
        let database = DialectStub {
            dialect: "postgres",
            statements: Mutex::new(Vec::new()),
        };
        let query = b"name=alpha,beta&price.from=10&note.null=true&search=50%25&ordering=-name";
        let result = execute_read_with(&filtered_plan(false), &database, &[], query, None).await;
        let NativeRead::Reply { status, body } = result else {
            panic!("filtered native list did not encode");
        };
        assert_eq!(status, 200);
        assert_eq!(
            body,
            br#"{"items":[{"id":4,"name":"alpha"}],"page":1,"page_size":20,"total":1,"pages":1}"#
        );
        let statements = database.statements.lock().unwrap();
        assert_eq!(statements.len(), 2);
        assert!(statements[0].0.contains("WHERE"));
        assert!(statements[1]
            .0
            .contains(r#"ORDER BY "name" DESC, "id" ASC"#));
        // Search term "50%" must be LIKE-escaped on PostgreSQL.
        assert!(statements[1].1.contains(&Value::Text("%50\\%%".to_owned())));
    }

    #[tokio::test]
    async fn filtered_list_defers_on_invalid_filter_or_sqlite_decimal_range_and_order() {
        let pg = DialectStub {
            dialect: "postgres",
            statements: Mutex::new(Vec::new()),
        };
        for query in [
            b"unknown=1".as_slice(),
            b"name=a&name=b",
            b"price.from=notanumber",
            b"ordering=unknown",
        ] {
            let result = execute_read_with(&filtered_plan(false), &pg, &[], query, None).await;
            assert!(matches!(result, NativeRead::Defer), "{query:?}");
        }
        assert!(pg.statements.lock().unwrap().is_empty());

        let sqlite = DialectStub {
            dialect: "sqlite",
            statements: Mutex::new(Vec::new()),
        };
        for query in [b"price.from=1.50".as_slice(), b"ordering=price"] {
            let result = execute_read_with(&filtered_plan(true), &sqlite, &[], query, None).await;
            assert!(matches!(result, NativeRead::Defer), "{query:?}");
        }
        assert!(sqlite.statements.lock().unwrap().is_empty());
    }
}
