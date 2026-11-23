//! Integration tests against a disposable MySQL or MariaDB server.
//!
//! Set `HINOKI_TEST_MYSQL_URL` with an explicit `require_ssl=false` only for a
//! local disposable instance. These tests never create a real account or send
//! any messages.

use std::time::Duration;

use hinoki_orm::{
    query::{compile_select_with, Dialect, Filter, Op, Select},
    Database, ErrorKind, Value,
};
use tokio::time::{sleep, timeout};
use uuid::Uuid;

fn url() -> Option<String> {
    let value = std::env::var("HINOKI_TEST_MYSQL_URL").ok();
    if value.is_none() {
        eprintln!("SKIPPED: HINOKI_TEST_MYSQL_URL is not set");
    }
    value
}

async fn database(table: &str, max_size: usize) -> Option<Database> {
    let db = Database::connect(&url()?, max_size, Duration::from_millis(150)).unwrap();
    db.execute(&format!("DROP TABLE IF EXISTS `{table}`"), &[])
        .await
        .unwrap();
    db.execute(
        &format!(
            "CREATE TABLE `{table}` (`id` BINARY(16) PRIMARY KEY, `slug` VARCHAR(80) UNIQUE NOT NULL, \
             `n` BIGINT, `at` DATETIME(6), `data` JSON, `flag` BOOLEAN, `blob` BLOB, `ratio` DOUBLE)"
        ),
        &[],
    )
    .await
    .unwrap();
    Some(db)
}

#[tokio::test]
async fn typed_values_round_trip_and_pool_recovers_after_errors() {
    let Some(db) = database("hinoki_mysql_values", 2).await else {
        return;
    };
    let is_mariadb = url().is_some_and(|url| url.starts_with("mariadb://"));
    assert_eq!(db.dialect(), if is_mariadb { "mariadb" } else { "mysql" });
    let id = Uuid::new_v4();
    let now = time::OffsetDateTime::now_utc();
    let values = [
        Value::Uuid(id),
        Value::Text("hello".into()),
        Value::Int(7),
        Value::Timestamp(now),
        Value::Json(serde_json::json!({"a": [1, 2]})),
        Value::Bool(true),
        Value::Bytes(vec![1, 2, 3]),
        Value::Float(2.5),
    ];
    assert_eq!(
        db.execute(
            "INSERT INTO `hinoki_mysql_values` VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            &values,
        )
        .await
        .unwrap(),
        1
    );
    let rows = db
        .fetch(
            "SELECT * FROM `hinoki_mysql_values` WHERE `id` = ?",
            &[Value::Uuid(id)],
        )
        .await
        .unwrap();
    let row: std::collections::HashMap<_, _> = rows[0].iter().cloned().collect();
    assert_eq!(row["id"], Value::Uuid(id));
    assert_eq!(row["n"], Value::Int(7));
    // MariaDB exposes its JSON-as-LONGTEXT alias as bytes in the generic row
    // API; the typed Python Model layer decodes declared dict/list fields.
    let expected_json = if is_mariadb {
        Value::Bytes(br#"{"a":[1,2]}"#.to_vec())
    } else {
        Value::Json(serde_json::json!({"a": [1, 2]}))
    };
    assert_eq!(row["data"], expected_json);
    assert_eq!(row["flag"], Value::Bool(true));
    assert_eq!(row["blob"], Value::Bytes(vec![1, 2, 3]));
    assert_eq!(row["ratio"], Value::Float(2.5));
    let Value::Timestamp(back) = &row["at"] else {
        panic!("timestamp expected")
    };
    assert!((*back - now).whole_microseconds().abs() < 2);

    sleep(Duration::from_millis(20)).await;
    let (size, idle) = db.status();
    assert!(size >= 1);
    assert!(idle >= 1 && idle <= size);
    let transaction = db.begin().await.unwrap();
    assert_eq!(db.status(), (size, idle - 1));
    transaction.rollback().await.unwrap();

    let query = Select {
        table: "hinoki_mysql_values".into(),
        columns: vec!["id".into()],
        joins: vec![],
        filters: vec![Filter {
            column: "slug,n".into(),
            op: Op::ContainsAny,
            values: vec![Value::Text("hello".into())],
        }],
        subfilters: vec![],
        keyset: None,
        order: vec![],
        limit: None,
        offset: None,
        count: false,
    };
    let (sql, params) = compile_select_with(&query, Dialect::Mysql).unwrap();
    assert_eq!(db.fetch(&sql, &params).await.unwrap().len(), 1);

    let mut offset_query = query;
    offset_query.filters.clear();
    offset_query.offset = Some(1);
    let (sql, params) = compile_select_with(&offset_query, Dialect::Mysql).unwrap();
    assert!(db.fetch(&sql, &params).await.unwrap().is_empty());

    let duplicate = db
        .execute(
            "INSERT INTO `hinoki_mysql_values` (`id`, `slug`) VALUES (?, ?)",
            &[Value::Uuid(Uuid::new_v4()), Value::Text("hello".into())],
        )
        .await
        .unwrap_err();
    assert_eq!(duplicate.kind, ErrorKind::Unique);
    assert_eq!(db.fetch("SELECT 1", &[]).await.unwrap().len(), 1);
    db.close();
}

#[tokio::test]
async fn transaction_drop_and_pool_wait_are_bounded() {
    let Some(db) = database("hinoki_mysql_tx", 1).await else {
        return;
    };
    db.execute(
        "INSERT INTO `hinoki_mysql_tx` (`id`, `slug`) VALUES (?, ?)",
        &[Value::Uuid(Uuid::new_v4()), Value::Text("first".into())],
    )
    .await
    .unwrap();

    let tx = db.begin().await.unwrap();
    let wait = timeout(Duration::from_millis(80), db.fetch("SELECT 1", &[])).await;
    assert!(wait.is_err());
    drop(tx);
    sleep(Duration::from_millis(50)).await;
    assert_eq!(db.fetch("SELECT 1", &[]).await.unwrap().len(), 1);
    db.close();
}

#[tokio::test]
async fn cancelled_statement_discards_the_connection_and_pool_recovers() {
    let Some(db) = database("hinoki_mysql_cancel", 1).await else {
        return;
    };
    let result = timeout(Duration::from_millis(40), db.fetch("SELECT SLEEP(1)", &[])).await;
    assert!(result.is_err());
    assert_eq!(db.fetch("SELECT 1", &[]).await.unwrap().len(), 1);
    db.close();
}

#[tokio::test]
async fn migration_transaction_holds_and_releases_a_database_lock() {
    let Some(db) = database("hinoki_mysql_lock", 2).await else {
        return;
    };
    let tx = db.begin_migration().await.unwrap();
    let rows = tx
        .fetch(
            "SELECT IS_USED_LOCK(CONCAT('h', LEFT(SHA2(DATABASE(), 256), 63))) AS owner, CONNECTION_ID() AS current",
            &[],
        )
        .await
        .unwrap();
    assert_eq!(rows[0][0].1, rows[0][1].1);
    tx.commit().await.unwrap();
    let rows = db
        .fetch(
            "SELECT IS_USED_LOCK(CONCAT('h', LEFT(SHA2(DATABASE(), 256), 63))) AS owner",
            &[],
        )
        .await
        .unwrap();
    assert_eq!(rows[0][0].1, Value::Null);
    db.close();
}

#[tokio::test]
async fn execute_scalar_returns_last_insert_id_of_the_branch_taken() {
    let Some(db) = database("hinoki_mysql_scalar_base", 2).await else {
        return;
    };
    db.execute("DROP TABLE IF EXISTS `hinoki_mysql_scalar`", &[])
        .await
        .unwrap();
    db.execute(
        "CREATE TABLE `hinoki_mysql_scalar` (`k` VARCHAR(8) NOT NULL PRIMARY KEY, `v` BIGINT NOT NULL)",
        &[],
    )
    .await
    .unwrap();
    // No AUTO_INCREMENT column: the value comes from LAST_INSERT_ID(expr) alone.
    let upsert = "INSERT INTO `hinoki_mysql_scalar` (`k`, `v`) VALUES (?, LAST_INSERT_ID(?)) \
                  ON DUPLICATE KEY UPDATE `v` = LAST_INSERT_ID(IF(`v` < ?, `v` + 1, `v`))";
    let db = &db;
    let hit = |limit: i64| async move {
        db.execute_scalar(
            upsert,
            &[Value::Text("a".into()), Value::Int(1), Value::Int(limit)],
        )
        .await
    };
    assert_eq!(hit(3).await.unwrap(), Some(1), "insert branch");
    assert_eq!(hit(3).await.unwrap(), Some(2), "update branch, row changed");
    assert_eq!(hit(3).await.unwrap(), Some(3), "update branch, row changed");
    for _ in 0..3 {
        assert_eq!(
            hit(3).await.unwrap(),
            Some(3),
            "update branch, row unchanged"
        );
    }
    let miss = db
        .execute_scalar(
            "UPDATE `hinoki_mysql_scalar` SET `v` = LAST_INSERT_ID(`v` + 1) WHERE `k` = ?",
            &[Value::Text("zz".into())],
        )
        .await
        .unwrap();
    assert_eq!(miss, None, "no matching row");
    db.execute("DROP TABLE `hinoki_mysql_scalar`", &[])
        .await
        .unwrap();
    db.close();
}
