//! Integration tests against a disposable PostgreSQL.
//!
//! Set `HINOKI_TEST_DATABASE_URL` (for example
//! `postgres://postgres:hinoki@127.0.0.1:54399/hinoki`). Without it every test
//! prints SKIPPED and passes, so a green run without a database proves nothing.

use std::time::Duration;

use hinoki_orm::{Database, ErrorKind, Value};
use tokio::time::timeout;
use uuid::Uuid;

fn url() -> Option<String> {
    let url = std::env::var("HINOKI_TEST_DATABASE_URL").ok();
    if url.is_none() {
        eprintln!("SKIPPED: HINOKI_TEST_DATABASE_URL is not set");
    }
    url
}

async fn database(table: &str, max_size: usize) -> Option<Database> {
    let db = Database::connect(&url()?, max_size, Duration::from_millis(300)).unwrap();
    db.execute(&format!("DROP TABLE IF EXISTS {table}"), &[])
        .await
        .unwrap();
    db.execute(
        &format!(
            "CREATE TABLE {table} (id uuid PRIMARY KEY, slug text UNIQUE NOT NULL, n int, \
             at timestamptz, data jsonb, flag bool, blob bytea, ratio float8)"
        ),
        &[],
    )
    .await
    .unwrap();
    Some(db)
}

fn insert(table: &str) -> String {
    format!("INSERT INTO {table} (id, slug, n) VALUES ($1, $2, $3)")
}

#[tokio::test]
async fn values_round_trip_through_typed_columns() {
    let Some(db) = database("rt_values", 2).await else {
        return;
    };
    let id = Uuid::new_v4();
    let now = chrono::Utc::now();
    let sql = "INSERT INTO rt_values (id, slug, n, at, data, flag, blob, ratio) \
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)";
    let args = [
        Value::Uuid(id),
        Value::Text("hello".into()),
        Value::Int(7),
        Value::Timestamp(now),
        Value::Json(serde_json::json!({"a": [1, 2]})),
        Value::Bool(true),
        Value::Bytes(vec![1, 2, 3]),
        Value::Float(2.5),
    ];
    assert_eq!(db.execute(sql, &args).await.unwrap(), 1);
    let rows = db
        .fetch("SELECT * FROM rt_values WHERE id = $1", &[Value::Uuid(id)])
        .await
        .unwrap();
    let row: std::collections::HashMap<_, _> = rows[0].iter().cloned().collect();
    assert_eq!(row["id"], Value::Uuid(id));
    assert_eq!(row["n"], Value::Int(7));
    assert_eq!(row["data"], Value::Json(serde_json::json!({"a": [1, 2]})));
    assert_eq!(row["blob"], Value::Bytes(vec![1, 2, 3]));
    assert_eq!(row["ratio"], Value::Float(2.5));
    let Value::Timestamp(back) = &row["at"] else {
        panic!("timestamp expected")
    };
    assert!((*back - now).num_microseconds().unwrap().abs() < 2);
}

#[tokio::test]
async fn null_and_type_mismatches_are_explicit() {
    let Some(db) = database("rt_nulls", 2).await else {
        return;
    };
    let id = Uuid::new_v4();
    db.execute(
        &insert("rt_nulls"),
        &[Value::Uuid(id), Value::Text("a".into()), Value::Null],
    )
    .await
    .unwrap();
    let rows = db.fetch("SELECT n FROM rt_nulls", &[]).await.unwrap();
    assert_eq!(rows[0][0].1, Value::Null);
    // A string cannot be sent as a uuid parameter: no implicit cast.
    let err = db
        .execute(
            &insert("rt_nulls"),
            &[
                Value::Text("x".into()),
                Value::Text("b".into()),
                Value::Int(1),
            ],
        )
        .await
        .unwrap_err();
    assert!(
        err.message.len() < 200 && !err.message.contains("INSERT"),
        "{err}"
    );
    // Out-of-range integers are refused, not truncated.
    let err = db
        .execute(
            &insert("rt_nulls"),
            &[
                Value::Uuid(Uuid::new_v4()),
                Value::Text("c".into()),
                Value::Int(i64::MAX),
            ],
        )
        .await
        .unwrap_err();
    assert!(!err.to_string().contains("INSERT"));
}

#[tokio::test]
async fn constraint_violations_are_typed_and_do_not_leak_values() {
    let Some(db) = database("rt_errors", 2).await else {
        return;
    };
    let args = |slug: &str| {
        [
            Value::Uuid(Uuid::new_v4()),
            Value::Text(slug.into()),
            Value::Int(1),
        ]
    };
    db.execute(&insert("rt_errors"), &args("secret-slug"))
        .await
        .unwrap();
    let err = db
        .execute(&insert("rt_errors"), &args("secret-slug"))
        .await
        .unwrap_err();
    assert_eq!(err.kind, ErrorKind::Unique);
    assert_eq!(err.constraint.as_deref(), Some("rt_errors_slug_key"));
    assert!(!err.to_string().contains("secret-slug"));
    let err = db
        .execute(
            &insert("rt_errors"),
            &[Value::Uuid(Uuid::new_v4()), Value::Null, Value::Int(1)],
        )
        .await
        .unwrap_err();
    assert_eq!(err.kind, ErrorKind::NotNull);
}

#[tokio::test]
async fn transactions_commit_roll_back_and_discard_when_dropped() {
    let Some(db) = database("rt_tx", 2).await else {
        return;
    };
    let count = |db: Database| async move {
        let rows = db
            .fetch("SELECT count(*)::int AS c FROM rt_tx", &[])
            .await
            .unwrap();
        rows[0][0].1.clone()
    };
    let row = |slug: &str| {
        [
            Value::Uuid(Uuid::new_v4()),
            Value::Text(slug.into()),
            Value::Int(1),
        ]
    };

    let tx = db.begin().await.unwrap();
    tx.execute(&insert("rt_tx"), &row("kept")).await.unwrap();
    tx.commit().await.unwrap();
    assert_eq!(count(db.clone()).await, Value::Int(1));

    let tx = db.begin().await.unwrap();
    tx.execute(&insert("rt_tx"), &row("rolled-back"))
        .await
        .unwrap();
    tx.rollback().await.unwrap();
    assert_eq!(count(db.clone()).await, Value::Int(1));

    let tx = db.begin().await.unwrap();
    tx.execute(&insert("rt_tx"), &row("dropped")).await.unwrap();
    drop(tx); // never returned to the pool in an open transaction
    assert_eq!(count(db.clone()).await, Value::Int(1));

    // A failed statement poisons the transaction; the caller rolls back.
    let tx = db.begin().await.unwrap();
    assert!(tx.execute(&insert("rt_tx"), &row("kept")).await.is_err());
    tx.rollback().await.unwrap();
    assert_eq!(count(db).await, Value::Int(1));
}

#[tokio::test]
async fn cancelled_query_leaves_the_pool_usable() {
    let Some(db) = database("rt_cancel", 1).await else {
        return;
    };
    let slow = db.fetch("SELECT pg_sleep(5)", &[]);
    assert!(timeout(Duration::from_millis(200), slow).await.is_err());
    // max_size is 1: the same connection (or a fresh one) must work now.
    let rows = timeout(
        Duration::from_secs(5),
        db.fetch("SELECT 1::int AS one", &[]),
    )
    .await;
    assert_eq!(rows.unwrap().unwrap()[0][0].1, Value::Int(1));
}

#[tokio::test]
async fn pool_exhaustion_times_out_instead_of_waiting_forever() {
    let Some(db) = database("rt_pool", 1).await else {
        return;
    };
    let tx = db.begin().await.unwrap();
    let err = db.fetch("SELECT 1", &[]).await.unwrap_err();
    assert_eq!(err.kind, ErrorKind::Pool);
    drop(tx);
}

#[tokio::test]
async fn unreachable_server_fails_the_startup_check_without_leaking_the_url() {
    if url().is_none() {
        return;
    }
    let db = Database::connect(
        "postgres://user:topsecret@127.0.0.1:1/none",
        1,
        Duration::from_millis(300),
    )
    .unwrap();
    let err = db.check().await.unwrap_err();
    assert!(!err.to_string().contains("topsecret"));
}
