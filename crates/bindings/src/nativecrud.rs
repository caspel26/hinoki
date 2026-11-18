//! GIL-free read execution for eligible generated CRUD routes.

use std::future::Future;
use std::pin::Pin;

use hinoki_orm::{Database, DbError, Value};

use crate::crudplan::CrudPlan;
use crate::request::parse_query;

#[derive(Debug)]
pub(crate) enum NativeRead {
    Reply { status: u16, body: Vec<u8> },
    Rows(Vec<Vec<(String, Value)>>, Option<(u64, u64, u64)>),
    Failure(DbError),
    NotFound,
    Timeout,
    Defer,
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
            vec![
                ("id".to_owned(), "id".to_owned(), "int".to_owned(), false),
                ("name".to_owned(), "name".to_owned(), "str".to_owned(), false),
            ],
            br#"{"error":"validation_error","request_id":"0123456789abcdeffedcba9876543210","details":[{"path":"body","code":"invalid","message":"invalid"}]}"#.to_vec(),
            Vec::new(),
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
