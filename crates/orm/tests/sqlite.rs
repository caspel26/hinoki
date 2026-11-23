//! Integration tests against a throwaway SQLite file.

use std::time::Duration;

use hinoki_orm::{Database, Value};
use tokio::time::timeout;
use uuid::Uuid;

const SLOW: &str =
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 2000000) \
                    SELECT count(*) FROM c";

fn database() -> (Database, std::path::PathBuf) {
    let path = std::env::temp_dir().join(format!("hinoki-orm-{}.sqlite3", Uuid::new_v4()));
    let db = Database::connect(
        &format!("sqlite://{}", path.display()),
        2,
        Duration::from_millis(300),
    )
    .unwrap();
    (db, path)
}

#[tokio::test]
async fn a_cancelled_statement_in_a_transaction_cannot_be_committed() {
    let (db, path) = database();
    db.execute(
        "CREATE TABLE notes (id integer PRIMARY KEY, title text)",
        &[],
    )
    .await
    .unwrap();
    let tx = db.begin().await.unwrap();
    tx.execute(
        "INSERT INTO notes (title) VALUES (?)",
        &[Value::Text("a".into())],
    )
    .await
    .unwrap();

    let cancelled = timeout(Duration::from_millis(10), tx.fetch(SLOW, &[])).await;
    assert!(cancelled.is_err(), "the statement was meant to time out");

    assert!(tx.commit().await.is_err());
    let rows = db.fetch("SELECT id FROM notes", &[]).await.unwrap();
    assert!(rows.is_empty());
    let _ = std::fs::remove_file(path);
}

#[tokio::test]
async fn execute_scalar_returns_the_first_returning_value() {
    let (db, path) = database();
    db.execute("CREATE TABLE counters (k text PRIMARY KEY, n integer)", &[])
        .await
        .unwrap();
    let upsert = "INSERT INTO counters (k, n) VALUES (?1, 1) \
                  ON CONFLICT (k) DO UPDATE SET n = n + 1 RETURNING n";
    let hit = || async { db.execute_scalar(upsert, &[Value::Text("a".into())]).await };
    assert_eq!(hit().await.unwrap(), Some(1));
    assert_eq!(hit().await.unwrap(), Some(2));
    let miss = db
        .execute_scalar(
            "UPDATE counters SET n = 5 WHERE k = ?1 RETURNING n",
            &[Value::Text("zz".into())],
        )
        .await
        .unwrap();
    assert_eq!(miss, None);
    let _ = std::fs::remove_file(path);
}
