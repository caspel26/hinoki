//! The limiter against every backend: SQLite always, PostgreSQL, MySQL and MariaDB
//! when `HINOKI_TEST_DATABASE_URL`, `HINOKI_TEST_MYSQL_URL` or
//! `HINOKI_TEST_MARIADB_URL` point at a disposable server.

use std::sync::Arc;
use std::time::Duration;

use hinoki_orm::Database;
use hinoki_ratelimit::gcra::{Decision, Quota, Verdict};
use hinoki_ratelimit::key::{KeyParts, Who};
use hinoki_ratelimit::observer::{Observer, Outcome};
use hinoki_ratelimit::store::{LimitError, Limiter, Limits, Rule};
use hinoki_ratelimit::TableName;
use uuid::Uuid;

const SECOND: i64 = 1_000_000;

struct Target {
    name: &'static str,
    db: Database,
    table: TableName,
}

async fn target(name: &'static str, url: &str) -> Target {
    let db = Database::connect(url, 8, Duration::from_secs(2)).unwrap();
    let table = TableName::new(&format!("rl_{}", Uuid::new_v4().simple())).unwrap();
    let sql = format!(
        "CREATE TABLE {} (bucket VARCHAR(64) NOT NULL PRIMARY KEY, state BIGINT NOT NULL)",
        table.as_str()
    );
    db.execute(&sql, &[]).await.unwrap();
    let index = format!("CREATE INDEX ix_{0}_state ON {0} (state)", table.as_str());
    db.execute(&index, &[]).await.unwrap();
    Target { name, db, table }
}

async fn targets() -> Vec<Target> {
    let path = std::env::temp_dir().join(format!("hinoki-rl-{}.sqlite3", Uuid::new_v4()));
    let mut found = vec![target("sqlite", &format!("sqlite://{}", path.display())).await];
    for (name, var) in [
        ("postgres", "HINOKI_TEST_DATABASE_URL"),
        ("mysql", "HINOKI_TEST_MYSQL_URL"),
        ("mariadb", "HINOKI_TEST_MARIADB_URL"),
    ] {
        match std::env::var(var) {
            Ok(url) => found.push(target(name, &url).await),
            Err(_) => eprintln!("SKIPPED: {var} is not set"),
        }
    }
    found
}

fn limits(max_rows: u64, sweep_every: u32) -> Limits {
    Limits {
        max_rows,
        timeout: Duration::from_secs(3),
        sweep_every,
    }
}

fn rule(limit: u32, window_s: i64) -> Rule {
    let quota = Quota::new(limit, window_s * SECOND).unwrap();
    Rule {
        quota,
        unknown: quota.scaled(10),
        cost: 1,
        fail_open: false,
    }
}

fn keyed(caller: &str) -> KeyParts<'_> {
    KeyParts {
        scope: "GET /items",
        who: Who::Caller(caller),
        tenant: None,
    }
}

async fn drop_table(target: &Target) {
    let sql = format!("DROP TABLE IF EXISTS {}", target.table.as_str());
    target.db.execute(&sql, &[]).await.unwrap();
}

#[tokio::test]
async fn a_burst_is_the_limit_and_keys_are_counted_apart() {
    for target in targets().await {
        let limiter = Limiter::database(target.db.clone(), &target.table, limits(1000, 1_000_000));
        let rule = rule(3, 60);
        let mut verdicts = Vec::new();
        for _ in 0..5 {
            verdicts.push(
                limiter
                    .check(&rule, &keyed("alice"), 10 * SECOND)
                    .await
                    .unwrap(),
            );
        }
        let allowed = verdicts
            .iter()
            .filter(|d| d.verdict == Verdict::Allowed)
            .count();
        assert_eq!(allowed, 3, "{}", target.name);
        assert_eq!(verdicts[3].retry_after_s, 20, "{}", target.name);
        let other = limiter
            .check(&rule, &keyed("bob"), 10 * SECOND)
            .await
            .unwrap();
        assert_eq!(other.verdict, Verdict::Allowed, "{}", target.name);
        let later = limiter
            .check(&rule, &keyed("alice"), 30 * SECOND)
            .await
            .unwrap();
        assert_eq!(later.verdict, Verdict::Allowed, "{}", target.name);
        drop_table(&target).await;
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn concurrent_hits_on_one_key_admit_exactly_the_limit() {
    for target in targets().await {
        let limiter = Arc::new(Limiter::database(
            target.db.clone(),
            &target.table,
            limits(1000, 1_000_000),
        ));
        let rule = rule(10, 60);
        let tasks: Vec<_> = (0..40)
            .map(|_| {
                let limiter = Arc::clone(&limiter);
                tokio::spawn(
                    async move { limiter.check(&rule, &keyed("shared"), 5 * SECOND).await },
                )
            })
            .collect();
        let mut allowed = 0;
        for task in tasks {
            if task.await.unwrap().unwrap().verdict == Verdict::Allowed {
                allowed += 1;
            }
        }
        assert_eq!(allowed, 10, "{}", target.name);
        drop_table(&target).await;
    }
}

#[tokio::test]
async fn the_sql_step_matches_the_in_memory_step() {
    for target in targets().await {
        let sql = Limiter::database(target.db.clone(), &target.table, limits(1000, 1_000_000));
        let memory = Limiter::memory(limits(1000, 1_000_000));
        let rule = rule(4, 7);
        let mut seed = 12345_u64;
        let mut now = SECOND;
        for _ in 0..120 {
            seed = seed
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            now += i64::try_from((seed >> 33) % 1_500_000).unwrap();
            let left: Decision = sql.check(&rule, &keyed("k"), now).await.unwrap();
            let right = memory.check(&rule, &keyed("k"), now).await.unwrap();
            assert_eq!(left, right, "{} at {now}", target.name);
        }
        drop_table(&target).await;
    }
}

#[tokio::test]
async fn sweeping_removes_expired_buckets_and_the_cap_refuses_new_keys_only() {
    for target in targets().await {
        let limiter = Limiter::database(target.db.clone(), &target.table, limits(3, 1));
        let rule = rule(1, 10);
        for name in ["a", "b", "c"] {
            limiter.check(&rule, &keyed(name), SECOND).await.unwrap();
        }
        let full = limiter.check(&rule, &keyed("d"), SECOND).await.unwrap();
        assert_eq!(full.verdict, Verdict::Refused, "{}", target.name);
        assert_eq!(full.retry_after_s, 10, "{}", target.name);
        let known = limiter.check(&rule, &keyed("a"), 2 * SECOND).await.unwrap();
        assert_eq!(known.verdict, Verdict::Refused, "{}", target.name);
        // After the window every bucket has expired and the next hit sweeps them.
        let after = 100 * SECOND;
        limiter.check(&rule, &keyed("a"), after).await.unwrap();
        let fresh = limiter.check(&rule, &keyed("d"), after).await.unwrap();
        assert_eq!(fresh.verdict, Verdict::Allowed, "{}", target.name);
        drop_table(&target).await;
    }
}

#[tokio::test]
async fn a_missing_table_fails_closed_or_open_by_rule_and_is_counted() {
    struct Count(std::sync::Mutex<Vec<Outcome>>);
    impl Observer for Count {
        fn decided(&self, outcome: Outcome) {
            self.0.lock().unwrap().push(outcome);
        }
    }
    for target in targets().await {
        drop_table(&target).await;
        let limiter = Limiter::database(target.db.clone(), &target.table, limits(10, 1000));
        let seen = Arc::new(Count(std::sync::Mutex::new(Vec::new())));
        limiter.attach(seen.clone());
        let closed = rule(1, 60);
        let error = limiter
            .check(&closed, &keyed("x"), SECOND)
            .await
            .unwrap_err();
        assert_eq!(
            error,
            LimitError::Unavailable { schema: true },
            "{}",
            target.name
        );
        let open = Rule {
            fail_open: true,
            ..closed
        };
        let decision = limiter.check(&open, &keyed("x"), SECOND).await.unwrap();
        assert!(
            decision.failed_open && decision.verdict == Verdict::Allowed,
            "{}",
            target.name
        );
        assert_eq!(
            *seen.0.lock().unwrap(),
            [Outcome::Unavailable, Outcome::FailedOpen]
        );
    }
}

#[tokio::test]
async fn an_unknown_client_gets_the_wider_quota() {
    let limiter = Limiter::memory(limits(100, 1_000_000));
    let rule = rule(2, 60);
    let parts = KeyParts {
        scope: "s",
        who: Who::Client(None),
        tenant: None,
    };
    let mut allowed = 0;
    for _ in 0..30 {
        if limiter.check(&rule, &parts, SECOND).await.unwrap().verdict == Verdict::Allowed {
            allowed += 1;
        }
    }
    assert_eq!(allowed, 20);
}

#[tokio::test]
async fn a_later_observer_replaces_the_earlier_one() {
    struct Count(std::sync::Mutex<usize>);
    impl Observer for Count {
        fn decided(&self, _: Outcome) {
            *self.0.lock().unwrap() += 1;
        }
    }
    let limiter = Limiter::memory(limits(100, 1_000_000));
    let (first, second) = (
        Arc::new(Count(std::sync::Mutex::new(0))),
        Arc::new(Count(std::sync::Mutex::new(0))),
    );
    let rule = rule(5, 60);

    limiter.attach(first.clone());
    limiter.check(&rule, &keyed("a"), SECOND).await.unwrap();
    limiter.attach(second.clone());
    limiter.check(&rule, &keyed("a"), SECOND).await.unwrap();

    assert_eq!(
        (*first.0.lock().unwrap(), *second.0.lock().unwrap()),
        (1, 1)
    );
}
