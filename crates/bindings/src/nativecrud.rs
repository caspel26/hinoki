//! GIL-free execution for eligible generated CRUD routes.

use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::time::Duration;

use hinoki_orm::query::{like_pattern, Filter, Op};
use hinoki_orm::{Database, DbError, ErrorKind, Transaction, Value};
use time::{OffsetDateTime, UtcOffset};
use uuid::Uuid;

use crate::crudplan::{retrieve_params, CallerContext, CrudPlan, ModelDefault, ModelDefaultValue};
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
    Create(Option<Value>, Vec<(String, Value)>, LinkWork),
    Update {
        id: Value,
        values: Vec<(String, Value)>,
        scope: Option<Value>,
        owner: Option<OwnerScope>,
        links: LinkWork,
    },
    Delete {
        id: Value,
        scope: Option<Value>,
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
    request_id: &str,
    caller: Option<CallerContext<'_>>,
) -> NativeRead {
    execute_read_with(plan, database, path_params, raw_query, request_id, caller).await
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
    caller: Option<CallerContext<'_>>,
) -> NativeWrite {
    let owner = match (plan.owner.as_deref(), caller) {
        (None, _) => None,
        (Some(column), Some(ctx)) => Some(OwnerScope {
            column: column.to_owned(),
            value: ctx.subject.to_owned(),
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
    (owner, caller): (Option<OwnerScope>, Option<CallerContext<'_>>),
) -> Result<PreparedWrite, NativeWrite> {
    if parse_query(raw_query, 1000).is_err() {
        return Err(http_error(
            request,
            400,
            "invalid_query",
            Some("malformed query string"),
        ));
    }
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
            let scope = resolve_scope(plan, caller)
                .map_err(|message| http_error(request, 403, "forbidden", Some(message)))?;
            Ok(PreparedWrite::Delete { id, scope, owner })
        }
        _ => Err(NativeWrite::Defer(body.map(<[u8]>::to_vec))),
    }
}

fn link_work(
    plan: &CrudPlan,
    sets: Vec<(usize, Vec<hinoki_orm::links::Canon>)>,
    caller: Option<CallerContext<'_>>,
    request: &PreparedRequest,
) -> Result<LinkWork, NativeWrite> {
    let active = sets
        .iter()
        .map(|(index, _)| *index)
        .chain(plan.read_links.iter().copied());
    ensure_link_scopes(plan, caller, active)
        .map_err(|message| http_error(request, 403, "forbidden", Some(message)))?;
    let owned_caller = caller.map(|ctx| (ctx.subject.to_owned(), ctx.claims.to_vec()));
    Ok(LinkWork {
        sets,
        caller: owned_caller,
    })
}

fn resolve_scope(
    plan: &CrudPlan,
    caller: Option<CallerContext<'_>>,
) -> Result<Option<Value>, &'static str> {
    let Some(spec) = &plan.scope else {
        return Ok(None);
    };
    spec.resolve(caller)
        .map(Some)
        .ok_or("You cannot access this item.")
}

fn ensure_link_scopes(
    plan: &CrudPlan,
    caller: Option<CallerContext<'_>>,
    indices: impl Iterator<Item = usize>,
) -> Result<(), &'static str> {
    for index in indices {
        if let Some(spec) = &plan.links[index].scope {
            if spec.resolve(caller).is_none() {
                return Err("You cannot access this item.");
            }
        }
    }
    Ok(())
}

fn prepare_create(
    plan: &CrudPlan,
    request: &PreparedRequest,
    body: &[u8],
    (owner, caller): (Option<OwnerScope>, Option<CallerContext<'_>>),
) -> Result<PreparedWrite, NativeWrite> {
    let json = decode_body(request, body)?;
    let schema = plan
        .create_schema()
        .ok_or(NativeWrite::Defer(Some(body.to_vec())))?;
    let parsed = validate_body(plan, schema, &json, request)?;
    let Some((mut values, sets)) = parsed else {
        return Err(NativeWrite::Defer(Some(body.to_vec())));
    };
    let scope = resolve_scope(plan, caller)
        .map_err(|message| http_error(request, 403, "forbidden", Some(message)))?;
    let links = link_work(plan, sets, caller, request)?;
    if !append_defaults(&mut values, plan.create_defaults(), plan) {
        return Err(NativeWrite::Defer(Some(body.to_vec())));
    }
    if let (Some(spec), Some(value)) = (&plan.scope, scope.as_ref()) {
        // The schema cannot carry the scope column, so this never overrides input.
        values.push((spec.column.clone(), value.clone()));
    }
    if let Some(owner) = owner {
        // The schema cannot carry the owner column, so this never overrides input.
        values.push((owner.column, Value::Text(owner.value)));
    }
    order_schema_values(&mut values, schema);
    ensure_create_requirements(plan, &values, body)?;
    Ok(PreparedWrite::Create(scope, values, links))
}

fn prepare_update(
    plan: &CrudPlan,
    request: &PreparedRequest,
    path_params: &[(String, String)],
    body: &[u8],
    (owner, caller): (Option<OwnerScope>, Option<CallerContext<'_>>),
) -> Result<PreparedWrite, NativeWrite> {
    let id = write_path_id(plan, path_params).ok_or(NativeWrite::Defer(Some(body.to_vec())))?;
    let json = decode_body(request, body)?;
    let schema = plan
        .update_schema()
        .ok_or(NativeWrite::Defer(Some(body.to_vec())))?;
    let Some((mut values, sets)) = validate_body(plan, schema, &json, request)? else {
        return Err(NativeWrite::Defer(Some(body.to_vec())));
    };
    let scope = resolve_scope(plan, caller)
        .map_err(|message| http_error(request, 403, "forbidden", Some(message)))?;
    let links = link_work(plan, sets, caller, request)?;
    if !append_defaults(&mut values, plan.update_defaults(), plan) {
        return Err(NativeWrite::Defer(Some(body.to_vec())));
    }
    order_schema_values(&mut values, schema);
    Ok(PreparedWrite::Update {
        id,
        values,
        scope,
        owner,
        links,
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
        ModelDefaultValue::Str(value) | ModelDefaultValue::Stored(value) => {
            Value::Text(value.clone())
        }
        ModelDefaultValue::Uuid4 => Value::Uuid(Uuid::new_v4()),
        ModelDefaultValue::Uuid7 => Value::Uuid(Uuid::now_v7()),
        ModelDefaultValue::Token(count) => Value::Text(crate::crypto::random_token(*count).ok()?),
        ModelDefaultValue::UtcNow => utc_now_value(),
    };
    kind_matches(kind?, &value).then_some(value)
}

/// The current time at the microsecond precision Python's clock has.
fn utc_now_value() -> Value {
    let now = OffsetDateTime::now_utc();
    let micros = (now.nanosecond() / 1_000) * 1_000;
    Value::Timestamp(now.replace_nanosecond(micros).unwrap_or(now))
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
            | ("decimal" | "date", Value::Text(_))
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
        Validated::Date(day) if kind == "date" => Value::Text(stored_date(day)),
        // Python stores `str(Decimal(text))`; any other spelling is left to it.
        Validated::Decimal(text)
            if kind == "decimal" && crate::crudplan::is_stored_decimal(&text) =>
        {
            Value::Text(text)
        }
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

/// `date.isoformat()` of a validated day (years 1 to 9999).
fn stored_date(day: time::Date) -> String {
    format!(
        "{:04}-{:02}-{:02}",
        day.year(),
        u8::from(day.month()),
        day.day()
    )
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
        PreparedWrite::Create(scope, values, links) => {
            create_in_transaction(
                plan,
                dialect,
                transaction,
                scope.as_ref(),
                values,
                &links,
                request_id,
            )
            .await
        }
        PreparedWrite::Update {
            id,
            values,
            scope,
            owner,
            links,
        } => {
            update_in_transaction(
                plan,
                dialect,
                transaction,
                (id, scope.as_ref(), owner.as_ref()),
                (values, &links),
                request_id,
            )
            .await
        }
        PreparedWrite::Delete { id, scope, owner } => {
            delete_in_transaction(
                plan,
                dialect,
                transaction,
                (id, scope.as_ref(), owner.as_ref()),
                request_id,
            )
            .await
        }
    }
}

async fn create_in_transaction(
    plan: &CrudPlan,
    dialect: &str,
    transaction: &Transaction,
    scope: Option<&Value>,
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
    let rows = transaction
        .fetch(read_back_sql, &retrieve_params(primary_key, scope))
        .await?;
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
        links.caller_context(),
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
    (id, scope, owner): (Value, Option<&Value>, Option<&OwnerScope>),
    (supplied, links): (Vec<(String, Value)>, &LinkWork),
    request_id: &str,
) -> Result<TransactionResult, DbError> {
    let visible_sql = required_sql(plan.retrieve_sql(dialect))?;
    let visible = transaction
        .fetch(visible_sql, &retrieve_params(id.clone(), scope))
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
    if let Some(owner_scope) = owner {
        if !owner_scope.owns(&visible[0]) {
            return forbidden_result(request_id, "You cannot change this item.");
        }
    }
    let values = add_update_timestamp(plan, supplied);
    if !values.is_empty() {
        let written =
            write_columns(plan, (dialect, transaction), (&id, scope, owner), values).await?;
        if written.is_empty() {
            return refused_result(plan, dialect, transaction, (id, scope, owner), request_id)
                .await;
        }
    }
    let owner_scope = link_owner_scope(plan, scope, owner);
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
            return refused_result(plan, dialect, transaction, (id, scope, owner), request_id)
                .await;
        }
        return refused_links(refused, request_id);
    }
    let read_back_sql = required_sql(plan.read_back_sql(dialect))?;
    let rows = transaction
        .fetch(read_back_sql, &retrieve_params(id.clone(), scope))
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
    (id, scope, owner): (&Value, Option<&Value>, Option<&OwnerScope>),
    values: Vec<(String, Value)>,
) -> Result<Vec<Vec<(String, Value)>>, DbError> {
    let columns = values
        .iter()
        .map(|(name, _)| name.clone())
        .collect::<Vec<_>>();
    let sql = plan
        .update_sql(dialect, &columns)
        .map_err(|_| DbError::new(ErrorKind::Other, "CRUD update SQL is unavailable"))?;
    let parameters = update_parameters(values, id, scope, owner);
    if is_mysql(dialect) {
        let bound = scope_parameters(id, scope, owner);
        mysql_update(transaction, plan, &sql, (&parameters, &bound)).await
    } else {
        transaction.fetch(&sql, &parameters).await
    }
}

fn update_parameters(
    values: Vec<(String, Value)>,
    id: &Value,
    scope: Option<&Value>,
    owner: Option<&OwnerScope>,
) -> Vec<Value> {
    let mut parameters = values
        .into_iter()
        .map(|(_, value)| value)
        .collect::<Vec<_>>();
    parameters.extend(scope_parameters(id, scope, owner));
    parameters
}

/// The rows whose links an update may lock and change: rows in the mandatory
/// row scope, the caller's own rows when owned, and only live rows of a
/// soft-delete model, as Python's `update_row` scopes them.
fn link_owner_scope(
    plan: &CrudPlan,
    row_scope: Option<&Value>,
    owner: Option<&OwnerScope>,
) -> hinoki_orm::links::Scope {
    let mut scope = hinoki_orm::links::Scope::default();
    if let (Some(spec), Some(value)) = (&plan.scope, row_scope) {
        scope.filters.push(Filter {
            column: spec.column.clone(),
            op: Op::Eq,
            values: vec![value.clone()],
        });
    }
    if let Some(owner_scope) = owner {
        scope.filters.push(Filter {
            column: owner_scope.column.clone(),
            op: Op::Eq,
            values: vec![Value::Text(owner_scope.value.clone())],
        });
    }
    if let Some(column) = &plan.soft_delete {
        scope.filters.push(Filter {
            column: column.clone(),
            op: Op::IsNull,
            values: Vec::new(),
        });
    }
    scope
}

fn scope_parameters(id: &Value, scope: Option<&Value>, owner: Option<&OwnerScope>) -> Vec<Value> {
    let mut parameters = vec![id.clone()];
    if let Some(value) = scope {
        parameters.push(value.clone());
    }
    if let Some(owner_scope) = owner {
        parameters.push(Value::Text(owner_scope.value.clone()));
    }
    parameters
}

/// MySQL has no RETURNING: the row is read back with the update's own key,
/// scope and owner filter, so a write that matched no row reads back as empty.
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
    (id, scope, owner): (Value, Option<&Value>, Option<&OwnerScope>),
    request_id: &str,
) -> Result<TransactionResult, DbError> {
    let sql = required_sql(plan.retrieve_sql(dialect))?;
    let rows = transaction
        .fetch(sql, &retrieve_params(id.clone(), scope))
        .await?;
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
    (id, scope, owner): (Value, Option<&Value>, Option<&OwnerScope>),
    request_id: &str,
) -> Result<TransactionResult, DbError> {
    let visible_sql = required_sql(plan.retrieve_sql(dialect))?;
    let visible = transaction
        .fetch(visible_sql, &retrieve_params(id.clone(), scope))
        .await?;
    if visible.is_empty() {
        return Ok(TransactionResult::NotFound(format!(
            "{} not found",
            plan.model_name
        )));
    }
    if let Some(owner_scope) = owner {
        if !owner_scope.owns(&visible[0]) {
            return forbidden_result(request_id, "You cannot change this item.");
        }
    }
    let delete_sql = required_sql(plan.delete_sql(dialect))?;
    let mut parameters: Vec<Value> = plan
        .soft_delete_assignments()
        .iter()
        .map(|_| utc_now_value())
        .collect();
    parameters.extend(scope_parameters(&id, scope, owner));
    if transaction.execute(delete_sql, &parameters).await? == 0 {
        return refused_result(plan, dialect, transaction, (id, scope, owner), request_id).await;
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

/// Undo the write's savepoint and release it: `ROLLBACK TO` keeps the
/// savepoint, so without the release every refused write would leave one open.
async fn rollback_savepoint(transaction: &Transaction, plan: &CrudPlan) {
    let Some(sql) = plan.rollback_savepoint_sql("mysql") else {
        return;
    };
    if transaction.execute(sql, &[]).await.is_err() {
        return;
    }
    if let Some(release) = plan.release_savepoint_sql("mysql") {
        let _ = transaction.execute(release, &[]).await;
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
    request_id: &str,
    caller: Option<CallerContext<'_>>,
) -> NativeRead {
    match plan.action.as_str() {
        "list" => execute_list(plan, database, raw_query, request_id, caller).await,
        "retrieve" => {
            execute_retrieve(plan, database, path_params, raw_query, request_id, caller).await
        }
        _ => NativeRead::Defer,
    }
}

fn read_forbidden(request_id: &str, message: &str) -> NativeRead {
    error_body(request_id, "forbidden", Some(message)).map_or(NativeRead::Defer, |body| {
        NativeRead::Reply { status: 403, body }
    })
}

fn resolve_read_scope(
    plan: &CrudPlan,
    request_id: &str,
    caller: Option<CallerContext<'_>>,
) -> Result<Option<Value>, NativeRead> {
    let scope =
        resolve_scope(plan, caller).map_err(|message| read_forbidden(request_id, message))?;
    ensure_link_scopes(plan, caller, plan.read_links.iter().copied())
        .map_err(|message| read_forbidden(request_id, message))?;
    Ok(scope)
}

async fn execute_list<D: ReadDatabase>(
    plan: &CrudPlan,
    database: &D,
    raw_query: &[u8],
    request_id: &str,
    caller: Option<CallerContext<'_>>,
) -> NativeRead {
    let Ok(pairs) = parse_query(raw_query, 1000) else {
        return NativeRead::Defer;
    };
    let Some(page) = page_from_pairs(&pairs, plan.page_size, plan.max_page_size) else {
        return NativeRead::Defer;
    };
    let dialect = database.dialect();
    let Some(filtered) = parse_list_filters(plan, dialect, pairs) else {
        return NativeRead::Defer;
    };
    let scope = match resolve_read_scope(plan, request_id, caller) {
        Ok(scope) => scope,
        Err(reply) => return reply,
    };
    let Some((count_sql, count_params, list_sql, list_params)) =
        list_statements(plan, dialect, scope, filtered, page)
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
    scope: Option<Value>,
    (filters, order): ListQuery,
    page: Page,
) -> Option<ListStatements> {
    if filters.is_empty() && order.is_empty() {
        let count_sql = plan.count_sql(dialect)?.to_owned();
        let count_params = scope.clone().into_iter().collect();
        let (list_sql, list_params) = plan.list_sql(dialect, page.size, page.offset, scope).ok()?;
        return Some((count_sql, count_params, list_sql, list_params));
    }
    let (count_sql, count_params) = plan
        .filtered_count_sql(dialect, scope.clone(), filters.clone())
        .ok()?;
    let (list_sql, list_params) = plan
        .filtered_list_sql(dialect, scope, filters, order, page.size, page.offset)
        .ok()?;
    Some((count_sql, count_params, list_sql, list_params))
}

fn parse_list_filters(
    plan: &CrudPlan,
    dialect: &str,
    pairs: Vec<(String, String)>,
) -> Option<ListQuery> {
    let Some(spec) = &plan.filters else {
        return Some((Vec::new(), Vec::new()));
    };
    let grouped = group_query_pairs(pairs);
    let reserved = ["page".to_owned(), "page_size".to_owned()];
    let parsed = spec.parse(&grouped, &reserved).ok()?;
    let filters = compile_filter_conditions(plan, dialect, spec.conditions(&parsed))?;
    let order = compile_filter_order(plan, dialect, parsed.order.as_deref())?;
    Some((filters, order))
}

/// Group repeated names in first-seen order; the index keeps it linear.
fn group_query_pairs(pairs: Vec<(String, String)>) -> Vec<(String, Vec<String>)> {
    let mut grouped: Vec<(String, Vec<String>)> = Vec::new();
    let mut index: HashMap<String, usize> = HashMap::with_capacity(pairs.len());
    for (name, value) in pairs {
        match index.get(&name) {
            Some(&at) => grouped[at].1.push(value),
            None => {
                index.insert(name.clone(), grouped.len());
                grouped.push((name, vec![value]));
            }
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
    request_id: &str,
    caller: Option<CallerContext<'_>>,
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
    let scope = match resolve_read_scope(plan, request_id, caller) {
        Ok(scope) => scope,
        Err(reply) => return reply,
    };
    let dialect = database.dialect();
    let Some(sql) = plan.retrieve_sql(dialect) else {
        return NativeRead::Defer;
    };
    let rows = match database
        .fetch(sql, &retrieve_params(value, scope.as_ref()))
        .await
    {
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

#[cfg(test)]
fn parse_page(raw: &[u8], default_size: u64, max_size: u64) -> Option<Page> {
    page_from_pairs(&parse_query(raw, 1000).ok()?, default_size, max_size)
}

fn page_from_pairs(pairs: &[(String, String)], default_size: u64, max_size: u64) -> Option<Page> {
    let mut page = None;
    let mut size = None;
    for (name, value) in pairs {
        match name.as_str() {
            "page" if page.is_none() => page = Some(parse_number(value, 1_000_000_000)?),
            "page_size" if size.is_none() => size = Some(parse_number(value, max_size)?),
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
                    // a retrieve binds its key first; a list binds only its window
                    let id = match values.first() {
                        Some(key) if !sql.contains(" OFFSET ") => key.clone(),
                        _ => Value::Int(2),
                    };
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
            None,
        )
        .unwrap()
    }

    #[test]
    fn link_writes_on_a_soft_delete_model_only_reach_live_owner_rows() {
        use hinoki_orm::query::Op;

        let owner = super::OwnerScope {
            column: "owner".to_owned(),
            value: "u1".to_owned(),
        };
        let mut soft = plan("update");
        soft.soft_delete = Some("deleted_at".to_owned());
        let scope = super::link_owner_scope(&soft, None, Some(&owner));
        let filters: Vec<_> = scope
            .filters
            .iter()
            .map(|f| (f.column.as_str(), f.op, f.values.clone()))
            .collect();
        assert_eq!(
            filters,
            vec![
                ("owner", Op::Eq, vec![Value::Text("u1".to_owned())]),
                ("deleted_at", Op::IsNull, Vec::new()),
            ]
        );
        let unowned = super::link_owner_scope(&soft, None, None);
        assert_eq!(unowned.filters.len(), 1);
        assert_eq!(unowned.filters[0].op, Op::IsNull);
        assert!(super::link_owner_scope(&plan("update"), None, None)
            .filters
            .is_empty());
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
        let result = execute_read_with(
            &plan("list"),
            &database,
            &[],
            b"page=2&page_size=1",
            "req-1",
            None,
        )
        .await;
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
        assert!(statements[1].0.ends_with("LIMIT $1 OFFSET $2"));
        assert_eq!(statements[1].1, [Value::Int(1), Value::Int(1)]);
    }

    #[tokio::test]
    async fn retrieve_uses_decoded_path_id_and_returns_one_projected_item() {
        let database = StubDatabase {
            statements: Mutex::new(Vec::new()),
        };
        let params = vec![("item_id".to_owned(), "%37".to_owned())];
        let result =
            execute_read_with(&plan("retrieve"), &database, &params, b"", "req-2", None).await;
        let NativeRead::Reply { status, body } = result else {
            panic!("valid native retrieve did not encode");
        };
        assert_eq!(status, 200);
        assert_eq!(body, br#"{"id":7,"name":"second"}"#);
        let statements = database.statements.lock().unwrap();
        assert_eq!(statements.len(), 1);
        assert!(statements[0].0.ends_with("LIMIT $2"));
        assert_eq!(statements[0].1, [Value::Int(7), Value::Int(2)]);
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
                execute_read_with(&plan("retrieve"), &database, &params, query, "req-3", None)
                    .await;
            assert!(matches!(result, NativeRead::Defer), "{query:?}");
        }
        assert!(database.statements.lock().unwrap().is_empty());
        let valid =
            execute_read_with(&plan("retrieve"), &database, &params, b"a=1", "req-4", None).await;
        assert!(matches!(valid, NativeRead::Reply { .. }));
    }

    #[test]
    fn list_sql_for_any_window_is_one_statement_with_bound_counts() {
        let plan = plan("list");
        for (dialect, quote, window) in [
            ("postgres", '"', "LIMIT $1 OFFSET $2"),
            ("sqlite", '"', "LIMIT ?1 OFFSET ?2"),
            ("mysql", '`', "LIMIT ? OFFSET ?"),
        ] {
            let q = |name: &str| format!("{quote}{name}{quote}");
            let expected = format!(
                "SELECT {}, {} FROM {} ORDER BY {} ASC {window}",
                q("id"),
                q("name"),
                q("items"),
                q("id")
            );
            for (size, offset) in [(7_i64, 14_i64), (7, 7_000), (100, 0)] {
                let (sql, params) = plan
                    .list_sql(dialect, size as u64, offset as u64, None)
                    .unwrap();
                assert_eq!(sql, expected);
                assert_eq!(params, [Value::Int(size), Value::Int(offset)]);
            }
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
        let result =
            execute_read_with(&filtered_plan(false), &database, &[], query, "req-5", None).await;
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
            let result =
                execute_read_with(&filtered_plan(false), &pg, &[], query, "req-6", None).await;
            assert!(matches!(result, NativeRead::Defer), "{query:?}");
        }
        assert!(pg.statements.lock().unwrap().is_empty());

        let sqlite = DialectStub {
            dialect: "sqlite",
            statements: Mutex::new(Vec::new()),
        };
        for query in [b"price.from=1.50".as_slice(), b"ordering=price"] {
            let result =
                execute_read_with(&filtered_plan(true), &sqlite, &[], query, "req-7", None).await;
            assert!(matches!(result, NativeRead::Defer), "{query:?}");
        }
        assert!(sqlite.statements.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn missing_row_scope_refuses_with_403_before_any_query() {
        use crate::crudplan::{CallerContext, RowScopeSpec, ScopeSource};

        let database = StubDatabase {
            statements: Mutex::new(Vec::new()),
        };
        let mut scoped_plan = plan("list");
        scoped_plan.scope = Some(RowScopeSpec {
            column: "name".to_owned(),
            source: ScopeSource::Claim("tenant_id".to_owned()),
        });
        let claims = vec![("tenant_id".to_owned(), None)];
        let caller = CallerContext {
            subject: "alice",
            claims: &claims,
        };
        let result =
            execute_read_with(&scoped_plan, &database, &[], b"", "req-scope", Some(caller)).await;
        let NativeRead::Reply { status, body } = result else {
            panic!("missing scope claim did not return 403");
        };
        assert_eq!(status, 403);
        let parsed: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(parsed["error"], "forbidden");
        assert!(database.statements.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn scoped_crud_executes_with_tenant_and_owner_parity_on_sqlite() {
        use std::time::Duration;

        use hinoki_orm::Database;

        use super::{
            create_in_transaction, delete_in_transaction, update_in_transaction, LinkWork,
            OwnerScope, TransactionResult,
        };
        use crate::crudplan::{CallerContext, RowScopeSpec, ScopeSource};

        let path =
            std::env::temp_dir().join(format!("hinoki_row_scope_{}.sqlite3", uuid::Uuid::new_v4()));
        let url = format!("sqlite://{}", path.display());
        let db = Database::connect(&url, 1, Duration::from_secs(2)).unwrap();
        let tx = db.begin().await.unwrap();
        tx.execute(
            "CREATE TABLE invoices (id INTEGER PRIMARY KEY, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL, title TEXT NOT NULL)",
            &[],
        )
        .await
        .unwrap();
        tx.commit().await.unwrap();

        let scoped_plan = |action: &str| {
            CrudPlan::new(
                action.to_owned(),
                "Invoice".to_owned(),
                "invoices".to_owned(),
                vec![
                    "id".to_owned(),
                    "tenant_id".to_owned(),
                    "owner_id".to_owned(),
                    "title".to_owned(),
                ],
                "id".to_owned(),
                Vec::new(),
                20,
                100,
                vec![
                    ("id".to_owned(), "int".to_owned(), false),
                    ("tenant_id".to_owned(), "str".to_owned(), false),
                    ("owner_id".to_owned(), "str".to_owned(), false),
                    ("title".to_owned(), "str".to_owned(), false),
                ],
                Vec::new(),
                Vec::new(),
                Vec::new(),
                vec![
                    "id".to_owned(),
                    "tenant_id".to_owned(),
                    "owner_id".to_owned(),
                    "title".to_owned(),
                ],
                vec!["title".to_owned()],
                None,
                vec![
                    ("id".to_owned(), "id".to_owned(), "int".to_owned(), false),
                    (
                        "tenant_id".to_owned(),
                        "tenant_id".to_owned(),
                        "str".to_owned(),
                        false,
                    ),
                    (
                        "owner_id".to_owned(),
                        "owner_id".to_owned(),
                        "str".to_owned(),
                        false,
                    ),
                    (
                        "title".to_owned(),
                        "title".to_owned(),
                        "str".to_owned(),
                        false,
                    ),
                ],
                br#"{"error":"validation_error","request_id":"0123456789abcdeffedcba9876543210","details":[{"path":"body","code":"invalid","message":"invalid"}]}"#.to_vec(),
                None,
                None,
                Some("owner_id".to_owned()),
                Some(RowScopeSpec {
                    column: "tenant_id".to_owned(),
                    source: ScopeSource::Claim("tenant_id".to_owned()),
                }),
            )
            .unwrap()
        };

        let tx = db.begin().await.unwrap();
        let tenant = Value::Text("acme".to_owned());
        let alice_create = create_in_transaction(
            &scoped_plan("create"),
            "sqlite",
            &tx,
            Some(&tenant),
            vec![
                ("id".to_owned(), Value::Int(1)),
                ("tenant_id".to_owned(), tenant.clone()),
                ("owner_id".to_owned(), Value::Text("alice".to_owned())),
                ("title".to_owned(), Value::Text("Acme Q4".to_owned())),
            ],
            &LinkWork::default(),
            "req-c",
        )
        .await
        .unwrap();
        assert!(matches!(
            alice_create,
            TransactionResult::Reply { status: 201, .. }
        ));
        tx.commit().await.unwrap();

        let acme_claims = vec![("tenant_id".to_owned(), Some("acme".to_owned()))];
        let globex_claims = vec![("tenant_id".to_owned(), Some("globex".to_owned()))];
        let alice_ctx = CallerContext {
            subject: "alice",
            claims: &acme_claims,
        };
        let bob_ctx = CallerContext {
            subject: "bob",
            claims: &globex_claims,
        };
        let list_plan = scoped_plan("list");
        let NativeRead::Reply {
            status: 200,
            body: alice_body,
        } = execute_read_with(&list_plan, &db, &[], b"", "req-l1", Some(alice_ctx)).await
        else {
            panic!("acme list failed");
        };
        let parsed: serde_json::Value = serde_json::from_slice(&alice_body).unwrap();
        assert_eq!(parsed["total"], 1);
        let NativeRead::Reply {
            status: 200,
            body: bob_body,
        } = execute_read_with(&list_plan, &db, &[], b"", "req-l2", Some(bob_ctx)).await
        else {
            panic!("globex list failed");
        };
        let parsed_bob: serde_json::Value = serde_json::from_slice(&bob_body).unwrap();
        assert_eq!(parsed_bob["total"], 0);

        let params = vec![("item_id".to_owned(), "1".to_owned())];
        let retrieve_plan = scoped_plan("retrieve");
        assert!(matches!(
            execute_read_with(&retrieve_plan, &db, &params, b"", "req-r1", Some(alice_ctx)).await,
            NativeRead::Reply { status: 200, .. }
        ));
        assert!(matches!(
            execute_read_with(&retrieve_plan, &db, &params, b"", "req-r2", Some(bob_ctx)).await,
            NativeRead::NotFound
        ));

        let aaron_owner = OwnerScope {
            column: "owner_id".to_owned(),
            value: "aaron".to_owned(),
        };
        let alice_owner = OwnerScope {
            column: "owner_id".to_owned(),
            value: "alice".to_owned(),
        };
        let globex = Value::Text("globex".to_owned());
        let tx = db.begin().await.unwrap();
        let aaron_res = update_in_transaction(
            &scoped_plan("update"),
            "sqlite",
            &tx,
            (Value::Int(1), Some(&tenant), Some(&aaron_owner)),
            (
                vec![("title".to_owned(), Value::Text("Hijack".to_owned()))],
                &LinkWork::default(),
            ),
            "req-u1",
        )
        .await
        .unwrap();
        assert!(matches!(aaron_res, TransactionResult::Forbidden(_)));
        let bob_res = update_in_transaction(
            &scoped_plan("update"),
            "sqlite",
            &tx,
            (Value::Int(1), Some(&globex), Some(&alice_owner)),
            (
                vec![("title".to_owned(), Value::Text("Hijack".to_owned()))],
                &LinkWork::default(),
            ),
            "req-u2",
        )
        .await
        .unwrap();
        assert!(matches!(bob_res, TransactionResult::NotFound(_)));
        let alice_upd = update_in_transaction(
            &scoped_plan("update"),
            "sqlite",
            &tx,
            (Value::Int(1), Some(&tenant), Some(&alice_owner)),
            (
                vec![("title".to_owned(), Value::Text("Updated".to_owned()))],
                &LinkWork::default(),
            ),
            "req-u3",
        )
        .await
        .unwrap();
        assert!(matches!(
            alice_upd,
            TransactionResult::Reply { status: 200, .. }
        ));
        let alice_del = delete_in_transaction(
            &scoped_plan("delete"),
            "sqlite",
            &tx,
            (Value::Int(1), Some(&tenant), Some(&alice_owner)),
            "req-d1",
        )
        .await
        .unwrap();
        assert!(matches!(
            alice_del,
            TransactionResult::Reply { status: 204, .. }
        ));
        tx.commit().await.unwrap();
        drop(db);
        let _ = std::fs::remove_file(path);
    }
}
