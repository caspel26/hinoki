//! TLS integration checks for a disposable PostgreSQL instance.
//!
//! Set `HINOKI_TEST_DATABASE_TLS_URL` to a TLS-enabled server URL with
//! `sslmode=require&sslrootcert=/path/to/test-ca.pem`.

use std::time::Duration;

use hinoki_orm::{Database, Value};
use tokio::io::{copy_bidirectional, AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::oneshot;
use tokio::time::{sleep, timeout};

fn tls_url() -> Option<String> {
    let url = std::env::var("HINOKI_TEST_DATABASE_TLS_URL").ok();
    if url.is_none() {
        eprintln!("SKIPPED: HINOKI_TEST_DATABASE_TLS_URL is not set");
    }
    url
}

async fn backend_activity(db: &Database, pid: i64) -> (String, Option<String>, String) {
    let rows = db
        .fetch(
            "SELECT state, wait_event, query FROM pg_stat_activity WHERE pid = $1",
            &[Value::Int(pid)],
        )
        .await
        .unwrap();
    let Value::Text(state) = &rows[0][0].1 else {
        panic!("expected a PostgreSQL activity state")
    };
    let wait_event = match &rows[0][1].1 {
        Value::Text(wait_event) => Some(wait_event.clone()),
        Value::Null => None,
        value => panic!("expected a PostgreSQL wait event or NULL, got {value:?}"),
    };
    let Value::Text(query) = &rows[0][2].1 else {
        panic!("expected a PostgreSQL query")
    };
    (state.clone(), wait_event, query.clone())
}

async fn backend_count(db: &Database, pid: i64) -> i64 {
    let rows = db
        .fetch(
            "SELECT count(*) FROM pg_stat_activity WHERE pid = $1",
            &[Value::Int(pid)],
        )
        .await
        .unwrap();
    let Value::Int(count) = rows[0][0].1 else {
        panic!("expected a backend count")
    };
    count
}

async fn stall_cancel_tls(
    listener: TcpListener,
    upstream: String,
    ready: oneshot::Sender<()>,
    closed: oneshot::Sender<()>,
) {
    let (mut client, _) = listener.accept().await.unwrap();
    let mut server = TcpStream::connect(&upstream).await.unwrap();
    tokio::spawn(async move {
        let _ = copy_bidirectional(&mut client, &mut server).await;
    });

    let (mut cancel, _) = listener.accept().await.unwrap();
    let mut request = [0; 8];
    cancel.read_exact(&mut request).await.unwrap();
    assert_eq!(
        u32::from_be_bytes(request[4..].try_into().unwrap()),
        80877103
    );
    cancel.write_all(b"S").await.unwrap();
    let _ = ready.send(());
    let mut bytes = [0; 4096];
    loop {
        match cancel.read(&mut bytes).await {
            Ok(0) | Err(_) => break,
            Ok(_) => {}
        }
    }
    let _ = closed.send(());
}

#[tokio::test]
async fn verified_tls_queries_and_cancellation_keep_the_pool_usable() {
    let Some(url) = tls_url() else { return };
    let db = Database::connect(&url, 1, Duration::from_secs(2)).unwrap();
    db.check().await.unwrap();

    let ssl = db
        .fetch(
            "SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()",
            &[],
        )
        .await
        .unwrap();
    assert_eq!(ssl[0][0].1, Value::Bool(true));

    let control = Database::connect(&url, 1, Duration::from_secs(2)).unwrap();
    let tx = db.begin().await.unwrap();
    let pid = tx.fetch("SELECT pg_backend_pid()", &[]).await.unwrap()[0][0]
        .1
        .clone();
    let Value::Int(pid) = pid else {
        panic!("expected PostgreSQL backend pid")
    };
    let mut last_activity = (String::new(), None, String::new());
    let active = timeout(Duration::from_secs(5), async {
        let query = tx.fetch("SELECT pg_sleep(30)", &[]);
        tokio::pin!(query);
        loop {
            tokio::select! {
                result = &mut query => panic!("sleep query ended before cancellation: {result:?}"),
                activity = backend_activity(&control, pid) => {
                    last_activity = activity.clone();
                    if activity.0 == "active"
                        && activity.1.as_deref() == Some("PgSleep")
                        && activity.2.contains("pg_sleep(30)")
                    {
                        break;
                    }
                }
            }
        }
    })
    .await;
    assert!(
        active.is_ok(),
        "the backend never entered the sleep wait: {last_activity:?}"
    );

    let stopped = timeout(Duration::from_secs(3), async {
        loop {
            if backend_activity(&control, pid).await.0 == "idle in transaction (aborted)" {
                break true;
            }
            sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap_or(false);
    assert!(stopped, "the server kept executing the cancelled query");
    assert!(tx.fetch("SELECT 1", &[]).await.is_err());
    tx.rollback().await.unwrap();
    assert_eq!(
        db.fetch("SELECT 1", &[]).await.unwrap()[0][0].1,
        Value::Int(1)
    );
}

#[tokio::test]
async fn stalled_cancel_tls_handshake_is_bounded() {
    let Some(url) = tls_url() else { return };
    if !url.contains("@localhost:") {
        eprintln!("SKIPPED: TLS integration URL must use localhost");
        return;
    }
    let authority = url
        .split_once('@')
        .and_then(|(_, rest)| rest.split('/').next())
        .unwrap();
    let upstream = format!("localhost:{}", authority.rsplit(':').next().unwrap());
    let listener = TcpListener::bind("localhost:0").await.unwrap();
    let proxy_port = listener.local_addr().unwrap().port();
    let proxy_url = url.replace(
        &format!("@{authority}/"),
        &format!("@localhost:{proxy_port}/"),
    );
    let (ready_tx, ready_rx) = oneshot::channel();
    let (closed_tx, closed_rx) = oneshot::channel();
    tokio::spawn(stall_cancel_tls(listener, upstream, ready_tx, closed_tx));

    let db = Database::connect(&proxy_url, 1, Duration::from_secs(2)).unwrap();
    let control = Database::connect(&url, 1, Duration::from_secs(2)).unwrap();
    let tx = db.begin().await.unwrap();
    let pid = tx.fetch("SELECT pg_backend_pid()", &[]).await.unwrap()[0][0]
        .1
        .clone();
    let Value::Int(pid) = pid else {
        panic!("expected PostgreSQL backend pid")
    };
    let active = timeout(Duration::from_secs(5), async {
        let query = tx.fetch("SELECT pg_sleep(30)", &[]);
        tokio::pin!(query);
        loop {
            tokio::select! {
                result = &mut query => panic!("sleep query ended before cancellation: {result:?}"),
                activity = backend_activity(&control, pid) => {
                    if activity.0 == "active"
                        && activity.1.as_deref() == Some("PgSleep")
                        && activity.2.contains("pg_sleep(30)")
                    {
                        break;
                    }
                }
            }
        }
    })
    .await;
    assert!(active.is_ok(), "the backend never entered the sleep query");
    assert!(timeout(Duration::from_secs(2), ready_rx)
        .await
        .unwrap()
        .is_ok());
    assert!(timeout(Duration::from_secs(3), closed_rx)
        .await
        .unwrap()
        .is_ok());
    assert!(
        tx.fetch("SELECT 1", &[]).await.is_err(),
        "timed-out transaction must be poisoned"
    );
    let cancelled = control
        .fetch("SELECT pg_cancel_backend($1)", &[Value::Int(pid)])
        .await
        .unwrap();
    assert_eq!(cancelled[0][0].1, Value::Bool(true));
    tx.rollback().await.unwrap();
    let stopped = timeout(Duration::from_secs(3), async {
        loop {
            if backend_count(&control, pid).await == 0 {
                break true;
            }
            sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap_or(false);
    assert!(stopped, "the discarded backend kept running its query");
}

#[tokio::test]
async fn require_and_verification_modes_open_verified_tls_connections() {
    let Some(url) = tls_url() else { return };
    for mode in ["require", "verify-ca", "verify-full"] {
        let mode_url = url.replace("sslmode=require", &format!("sslmode={mode}"));
        let db = Database::connect(&mode_url, 1, Duration::from_secs(2)).unwrap();
        db.check().await.unwrap();
    }
}

#[tokio::test]
async fn tls_rejects_a_hostname_mismatch() {
    let Some(url) = tls_url() else { return };
    if !url.contains("@localhost:") {
        eprintln!("SKIPPED: TLS integration URL must use localhost");
        return;
    }
    for mode in ["prefer", "require", "verify-ca", "verify-full"] {
        let wrong_host = url
            .replace("sslmode=require", &format!("sslmode={mode}"))
            .replacen("@localhost:", "@127.0.0.1:", 1);
        let db = Database::connect(&wrong_host, 1, Duration::from_secs(2)).unwrap();
        assert!(db.check().await.is_err(), "hostname accepted in {mode}");
    }
}

#[tokio::test]
async fn tls_modes_reject_an_untrusted_certificate() {
    let Some(url) = tls_url() else { return };
    let no_ca = url
        .split_once("&sslrootcert=")
        .map_or_else(|| url.clone(), |(before, _)| before.to_owned());
    for mode in ["prefer", "require", "verify-ca", "verify-full"] {
        let mode_url = no_ca.replace("sslmode=require", &format!("sslmode={mode}"));
        let db = Database::connect(&mode_url, 1, Duration::from_secs(2)).unwrap();
        assert!(
            db.check().await.is_err(),
            "untrusted certificate accepted in {mode}"
        );
    }
}

#[tokio::test]
async fn disable_uses_a_plain_connection_even_when_tls_is_available() {
    let Some(url) = tls_url() else { return };
    let base = url.split('?').next().unwrap_or(&url);
    let plain_url = format!("{base}?sslmode=disable");
    let db = Database::connect(&plain_url, 1, Duration::from_secs(2)).unwrap();
    db.check().await.unwrap();
    let ssl = db
        .fetch(
            "SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()",
            &[],
        )
        .await
        .unwrap();
    assert_eq!(ssl[0][0].1, Value::Bool(false));
}

#[tokio::test]
async fn tls_root_certificate_errors_do_not_echo_the_path() {
    let Some(url) = tls_url() else { return };
    let bad = url.replace("sslrootcert=", "sslrootcert=/path/that/does/not/exist/");
    let error = Database::connect(&bad, 1, Duration::from_secs(2))
        .err()
        .expect("an unavailable CA file must fail closed");
    assert!(!error.to_string().contains("/path/that/does/not/exist/"));
}

#[tokio::test]
async fn prefer_falls_back_only_when_the_server_declines_tls() {
    let Ok(plain_url) = std::env::var("HINOKI_TEST_DATABASE_URL") else {
        eprintln!("SKIPPED: HINOKI_TEST_DATABASE_URL is not set");
        return;
    };
    let separator = if plain_url.contains('?') { '&' } else { '?' };
    let prefer_url = format!("{plain_url}{separator}sslmode=prefer");
    let prefer = Database::connect(&prefer_url, 1, Duration::from_secs(2)).unwrap();
    prefer.check().await.unwrap();
    let ssl = prefer
        .fetch(
            "SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()",
            &[],
        )
        .await
        .unwrap();
    assert_eq!(ssl[0][0].1, Value::Bool(false));

    let require_url = format!("{plain_url}{separator}sslmode=require");
    let require = Database::connect(&require_url, 1, Duration::from_secs(2)).unwrap();
    assert!(require.check().await.is_err());
}
