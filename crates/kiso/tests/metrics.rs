//! Prometheus metrics over real loopback sockets: the series are labelled by
//! route pattern, method and status class, streamed bytes are counted, the
//! scrape listener refuses what it should, and workers' snapshots merge.

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use bytes::Bytes;
use kiso::metrics::Config;
use kiso::scrape::{publish, serve_metrics, Access, Endpoint, Shared};
use kiso::{
    serve, Answer, Backend, Delivery, Incoming, Networks, Pace, Registry, ServerConfig, Streamed,
};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{mpsc, watch};
use tokio::time::{sleep, timeout};

const WAIT: Duration = Duration::from_secs(5);
const ITEM_ACTION: u32 = 3;
const STREAM_ACTION: u32 = 4;

struct Routes;

impl Backend for Routes {
    async fn handle(&self, _request: Incoming) -> Answer {
        unreachable!("every request is delivered")
    }

    async fn deliver(&self, request: Incoming) -> Delivery {
        if request.path().starts_with("/items/") {
            request.set_trace_action(ITEM_ACTION);
            return Delivery::Whole(Answer {
                status: 200,
                headers: vec![(b"content-length".to_vec(), b"5".to_vec())],
                body: Bytes::from_static(b"hello"),
            });
        }
        if request.path() == "/stream" {
            request.set_trace_action(STREAM_ACTION);
            let (sender, chunks) = mpsc::channel(4);
            tokio::spawn(async move {
                let _ = sender.send(Ok(Bytes::from_static(b"ab"))).await;
                let _ = sender.send(Ok(Bytes::from_static(b"cde"))).await;
            });
            return Delivery::Streamed(Streamed {
                status: 200,
                headers: vec![(b"content-type".to_vec(), b"text/plain".to_vec())],
                chunks,
                pace: Pace::default(),
            });
        }
        Delivery::Whole(Answer {
            status: 404,
            headers: vec![(b"content-length".to_vec(), b"0".to_vec())],
            body: Bytes::new(),
        })
    }
}

fn registry(worker: u32) -> Arc<Registry> {
    let routes: Vec<(u32, Arc<str>)> = vec![
        (ITEM_ACTION, Arc::from("/items/{id}")),
        (STREAM_ACTION, Arc::from("/stream")),
    ];
    let config = Config {
        worker,
        ..Config::default()
    };
    Arc::new(Registry::new(config, &routes).unwrap())
}

async fn listen() -> (TcpListener, SocketAddr) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    (listener, addr)
}

async fn get(addr: SocketAddr, method: &str, path: &str, extra: &str) -> String {
    let mut stream = TcpStream::connect(addr).await.unwrap();
    let text = format!("{method} {path} HTTP/1.1\r\nhost: x\r\n{extra}connection: close\r\n\r\n");
    stream.write_all(text.as_bytes()).await.unwrap();
    let mut seen = Vec::new();
    timeout(WAIT, stream.read_to_end(&mut seen))
        .await
        .expect("response")
        .unwrap();
    String::from_utf8_lossy(&seen).into_owned()
}

fn scratch(name: &str) -> PathBuf {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let dir = std::env::temp_dir().join(format!(
        "hinoki-metrics-{name}-{}-{nanos}",
        std::process::id()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn access(path: &str) -> Access {
    Access {
        path: path.to_owned(),
        ..Access::default()
    }
}

#[tokio::test]
async fn series_follow_route_pattern_status_and_streamed_bytes() {
    let registry = registry(0);
    let (app, app_addr) = listen().await;
    let config = ServerConfig {
        metrics: Some(Arc::clone(&registry)),
        ..ServerConfig::default()
    };
    let (stop, receiver) = watch::channel(false);
    tokio::spawn(serve(app, Arc::new(Routes), config, None, receiver.clone()));
    let (scrape, scrape_addr) = listen().await;
    let endpoint = Arc::new(Endpoint::new(
        Arc::clone(&registry),
        access("/metrics"),
        None,
    ));
    tokio::spawn(serve_metrics(scrape, endpoint, receiver));

    get(app_addr, "GET", "/items/1", "").await;
    get(app_addr, "GET", "/items/2", "").await;
    get(app_addr, "GET", "/nowhere", "").await;
    let streamed = get(app_addr, "GET", "/stream", "").await;
    assert!(streamed.contains("cde"), "{streamed}");

    let page = get(scrape_addr, "GET", "/metrics", "").await;
    assert!(page.starts_with("HTTP/1.1 200"), "{page}");
    assert!(page.contains("text/plain; version=0.0.4"), "{page}");
    for line in [
        "hinoki_http_requests_total{route=\"/items/{id}\",method=\"GET\",status=\"2xx\",worker=\"0\"} 2",
        "hinoki_http_requests_total{route=\"\",method=\"GET\",status=\"4xx\",worker=\"0\"} 1",
        "hinoki_http_request_duration_seconds_count{route=\"/items/{id}\",method=\"GET\",worker=\"0\"} 2",
        "hinoki_http_response_bytes_total{route=\"/items/{id}\",worker=\"0\"} 10",
        "hinoki_http_response_bytes_total{route=\"/stream\",worker=\"0\"} 5",
    ] {
        assert!(page.contains(line), "missing {line}\n{page}");
    }
    assert!(!page.contains("/items/1"), "a path value became a label");
    let _ = stop.send(true);
}

#[tokio::test]
async fn the_scrape_listener_refuses_wrong_method_path_peer_and_token() {
    let (scrape, addr) = listen().await;
    let guarded = Access {
        token: Some("s3cret".to_owned()),
        ..access("/metrics")
    };
    let endpoint = Arc::new(Endpoint::new(registry(0), guarded, None));
    let (stop, receiver) = watch::channel(false);
    tokio::spawn(serve_metrics(scrape, endpoint, receiver));

    assert!(get(addr, "POST", "/metrics", "")
        .await
        .starts_with("HTTP/1.1 405"));
    assert!(get(addr, "GET", "/other", "")
        .await
        .starts_with("HTTP/1.1 404"));
    let denied = get(addr, "GET", "/metrics", "").await;
    assert!(denied.starts_with("HTTP/1.1 401"), "{denied}");
    let wrong = get(addr, "GET", "/metrics", "authorization: Bearer nope\r\n").await;
    assert!(wrong.starts_with("HTTP/1.1 401"), "{wrong}");
    let right = get(addr, "GET", "/metrics", "authorization: Bearer s3cret\r\n").await;
    assert!(right.starts_with("HTTP/1.1 200"), "{right}");
    let _ = stop.send(true);

    let (scrape, addr) = listen().await;
    let foreign = Access {
        allow: Some(Networks::parse(&["10.0.0.0/8"]).unwrap()),
        ..access("/metrics")
    };
    let endpoint = Arc::new(Endpoint::new(registry(0), foreign, None));
    let (stop, receiver) = watch::channel(false);
    tokio::spawn(serve_metrics(scrape, endpoint, receiver));
    assert!(get(addr, "GET", "/metrics", "")
        .await
        .starts_with("HTTP/1.1 403"));
    let _ = stop.send(true);
}

#[tokio::test]
async fn a_scrape_merges_the_fresh_snapshots_of_other_workers() {
    let dir = scratch("merge");
    let interval = Duration::from_millis(50);
    let peer = registry(1);
    peer.observe(
        peer.slot_of(Some(ITEM_ACTION)),
        0,
        200,
        Duration::from_millis(3),
    );
    let (stop, receiver) = watch::channel(false);
    let shared = |slot| Shared {
        dir: dir.clone(),
        slot,
        interval,
    };
    tokio::spawn(publish(Arc::clone(&peer), shared(1), receiver.clone()));
    sleep(Duration::from_millis(200)).await;

    let own = registry(0);
    let (scrape, addr) = listen().await;
    let endpoint = Arc::new(Endpoint::new(own, access("/metrics"), Some(shared(0))));
    tokio::spawn(serve_metrics(scrape, endpoint, receiver));
    let page = get(addr, "GET", "/metrics", "").await;
    assert!(page.contains("worker=\"0\""), "{page}");
    assert!(
        page.contains(
            "hinoki_http_requests_total{route=\"/items/{id}\",method=\"GET\",status=\"2xx\",worker=\"1\"} 1"
        ),
        "{page}"
    );
    assert_eq!(page.matches("# TYPE hinoki_http_requests_total").count(), 1);
    let _ = stop.send(true);
    let _ = std::fs::remove_dir_all(dir);
}
