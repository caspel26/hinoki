//! The connection layer over real loopback sockets, with a fake backend.

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use kiso::{serve, Answer, Backend, Closed, Incoming, ServerConfig};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{watch, Notify};
use tokio::task::JoinHandle;
use tokio::time::{sleep, timeout};

const WAIT: Duration = Duration::from_secs(5);

#[derive(Default)]
struct Fake {
    started: Notify,
    release: Notify,
}

fn answer(body: &str) -> Answer {
    Answer {
        status: 200,
        headers: vec![
            (b"content-type".to_vec(), b"text/plain".to_vec()),
            (
                b"content-length".to_vec(),
                body.len().to_string().into_bytes(),
            ),
        ],
        body: bytes::Bytes::copy_from_slice(body.as_bytes()),
    }
}

impl Backend for Fake {
    async fn handle(&self, request: Incoming) -> Answer {
        if let Some(early) = &request.refusal {
            return Answer {
                status: early.status(),
                headers: vec![(b"content-length".to_vec(), b"0".to_vec())],
                body: bytes::Bytes::new(),
            };
        }
        match request.path() {
            "/slow" => {
                self.started.notify_one();
                self.release.notified().await;
                answer("slow done")
            }
            "/stuck" => {
                self.started.notify_one();
                std::future::pending().await
            }
            "/switch" => Answer {
                status: 101,
                headers: vec![(b"upgrade".to_vec(), b"websocket".to_vec())],
                body: bytes::Bytes::new(),
            },
            "/big" => answer(&"x".repeat(64 * 1024 * 1024)),
            "/echo" => {
                let headers: Vec<String> = request
                    .headers
                    .iter()
                    .map(|(n, v)| {
                        format!("{}={}", n.as_str(), String::from_utf8_lossy(v.as_bytes()))
                    })
                    .collect();
                answer(&format!(
                    "{} {} {} {} {}",
                    request.method,
                    request.path(),
                    String::from_utf8_lossy(request.query()),
                    request.peer.ip(),
                    headers.join(";")
                ))
            }
            _ => answer("hello world"),
        }
    }
}

struct Running {
    addr: SocketAddr,
    backend: Arc<Fake>,
    stop: watch::Sender<bool>,
    task: JoinHandle<Closed>,
}

async fn start(config: ServerConfig) -> Running {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let backend = Arc::new(Fake::default());
    let (stop, receiver) = watch::channel(false);
    let task = tokio::spawn(serve(
        listener,
        Arc::clone(&backend),
        config,
        None,
        receiver,
    ));
    Running {
        addr,
        backend,
        stop,
        task,
    }
}

async fn connect(running: &Running) -> TcpStream {
    TcpStream::connect(running.addr).await.unwrap()
}

/// Read one response: head text and exactly `content-length` body bytes.
async fn read_response(stream: &mut TcpStream, expect_body: bool) -> (String, Vec<u8>) {
    let mut raw = Vec::new();
    let mut byte = [0u8; 1];
    while !raw.ends_with(b"\r\n\r\n") {
        timeout(WAIT, stream.read_exact(&mut byte))
            .await
            .expect("response head")
            .expect("connection open");
        raw.push(byte[0]);
    }
    let head = String::from_utf8(raw).unwrap();
    let length = head
        .to_ascii_lowercase()
        .lines()
        .find_map(|l| l.strip_prefix("content-length: ").map(str::to_owned))
        .map_or(0, |v| v.parse::<usize>().unwrap());
    let mut body = vec![0u8; if expect_body { length } else { 0 }];
    timeout(WAIT, stream.read_exact(&mut body))
        .await
        .expect("response body")
        .unwrap();
    (head, body)
}

async fn is_closed(stream: &mut TcpStream, within: Duration) -> bool {
    let mut byte = [0u8; 1];
    matches!(
        timeout(within, stream.read(&mut byte)).await,
        Ok(Ok(0)) | Ok(Err(_))
    )
}

async fn get(stream: &mut TcpStream, path: &str) -> (String, Vec<u8>) {
    let request = format!("GET {path} HTTP/1.1\r\nhost: t\r\n\r\n");
    stream.write_all(request.as_bytes()).await.unwrap();
    read_response(stream, true).await
}

async fn finish(running: Running) -> Closed {
    running.stop.send(true).unwrap();
    timeout(WAIT, running.task).await.unwrap().unwrap()
}

#[tokio::test]
async fn serves_a_get_and_keeps_the_connection() {
    let running = start(ServerConfig::default()).await;
    let mut stream = connect(&running).await;
    for _ in 0..2 {
        let (head, body) = get(&mut stream, "/health").await;
        assert!(head.starts_with("HTTP/1.1 200 OK"), "{head}");
        assert_eq!(body, b"hello world");
    }
    assert_eq!(finish(running).await, Closed { aborted: 0 });
}

#[tokio::test]
async fn head_keeps_the_length_and_sends_no_body() {
    let running = start(ServerConfig::default()).await;
    let mut stream = connect(&running).await;
    stream
        .write_all(b"HEAD /health HTTP/1.1\r\nhost: t\r\n\r\n")
        .await
        .unwrap();
    let (head, _) = read_response(&mut stream, false).await;
    assert!(
        head.to_ascii_lowercase().contains("content-length: 11"),
        "{head}"
    );
    // Any body byte would break the framing of the next response.
    let (head, body) = get(&mut stream, "/health").await;
    assert!(head.starts_with("HTTP/1.1 200 OK"), "{head}");
    assert_eq!(body, b"hello world");
    finish(running).await;
}

#[tokio::test]
async fn pipelined_requests_are_answered_in_order() {
    let running = start(ServerConfig::default()).await;
    let mut stream = connect(&running).await;
    stream
        .write_all(
            b"GET /echo?n=1 HTTP/1.1\r\nhost: t\r\n\r\nGET /echo?n=2 HTTP/1.1\r\nhost: t\r\n\r\n",
        )
        .await
        .unwrap();
    let (_, first) = read_response(&mut stream, true).await;
    let (_, second) = read_response(&mut stream, true).await;
    assert!(String::from_utf8(first).unwrap().contains(" n=1 "));
    assert!(String::from_utf8(second).unwrap().contains(" n=2 "));
    finish(running).await;
}

#[tokio::test]
async fn the_backend_sees_the_raw_target_and_lowercase_headers() {
    let running = start(ServerConfig::default()).await;
    let mut stream = connect(&running).await;
    stream
        .write_all(b"GET /echo?a=%41&b HTTP/1.1\r\nHost: t\r\nX-One: 1\r\nX-One: 2\r\n\r\n")
        .await
        .unwrap();
    let (_, body) = read_response(&mut stream, true).await;
    let text = String::from_utf8(body).unwrap();
    assert!(text.starts_with("GET /echo a=%41&b 127.0.0.1 "), "{text}");
    assert!(text.contains("host=t;x-one=1;x-one=2"), "{text}");
    finish(running).await;
}

#[tokio::test]
async fn a_request_whose_body_was_not_read_closes_the_connection() {
    let running = start(ServerConfig::default()).await;
    let mut stream = connect(&running).await;
    stream
        .write_all(b"POST /health HTTP/1.1\r\nhost: t\r\ncontent-length: 4\r\n\r\nabcd")
        .await
        .unwrap();
    let (head, _) = read_response(&mut stream, true).await;
    assert!(head.starts_with("HTTP/1.1 200"), "{head}");
    assert!(
        head.to_ascii_lowercase().contains("connection: close"),
        "{head}"
    );
    assert!(is_closed(&mut stream, WAIT).await);
    finish(running).await;
}

#[tokio::test]
async fn a_post_without_a_body_reaches_the_backend() {
    let running = start(ServerConfig::default()).await;
    let mut stream = connect(&running).await;
    stream
        .write_all(b"POST /echo HTTP/1.1\r\nhost: t\r\ncontent-length: 0\r\n\r\n")
        .await
        .unwrap();
    let (head, body) = read_response(&mut stream, true).await;
    assert!(head.starts_with("HTTP/1.1 200"), "{head}");
    assert!(String::from_utf8(body).unwrap().starts_with("POST /echo"));
    finish(running).await;
}

#[tokio::test]
async fn the_connection_limit_holds_further_clients_until_a_slot_frees() {
    let config = ServerConfig {
        max_connections: 1,
        ..ServerConfig::default()
    };
    let running = start(config).await;
    let mut first = connect(&running).await;
    get(&mut first, "/health").await;
    let mut second = connect(&running).await;
    second
        .write_all(b"GET /health HTTP/1.1\r\nhost: t\r\n\r\n")
        .await
        .unwrap();
    let mut byte = [0u8; 1];
    assert!(
        timeout(Duration::from_millis(300), second.read(&mut byte))
            .await
            .is_err(),
        "the second client was served over the limit"
    );
    drop(first);
    let (head, _) = read_response(&mut second, true).await;
    assert!(head.starts_with("HTTP/1.1 200"), "{head}");
    finish(running).await;
}

#[tokio::test]
async fn a_silent_client_is_closed_by_the_header_read_timeout() {
    let config = ServerConfig {
        header_read_timeout: Duration::from_millis(300),
        ..ServerConfig::default()
    };
    let running = start(config).await;
    let mut silent = connect(&running).await;
    assert!(is_closed(&mut silent, WAIT).await);
    finish(running).await;
}

#[tokio::test]
async fn an_idle_kept_alive_connection_is_closed_by_the_same_timeout() {
    let config = ServerConfig {
        header_read_timeout: Duration::from_millis(300),
        ..ServerConfig::default()
    };
    let running = start(config).await;
    let mut stream = connect(&running).await;
    get(&mut stream, "/health").await;
    assert!(is_closed(&mut stream, WAIT).await);
    finish(running).await;
}

#[tokio::test]
async fn an_upgrade_request_is_answered_as_plain_http() {
    let running = start(ServerConfig::default()).await;
    let mut stream = connect(&running).await;
    stream
        .write_all(
            b"GET /health HTTP/1.1\r\nhost: t\r\nconnection: upgrade\r\nupgrade: websocket\r\n\r\n",
        )
        .await
        .unwrap();
    let (head, _) = read_response(&mut stream, true).await;
    assert!(head.starts_with("HTTP/1.1 200"), "{head}");
    // Still HTTP afterwards: the connection was never handed over.
    let (again, _) = get(&mut stream, "/health").await;
    assert!(again.starts_with("HTTP/1.1 200"), "{again}");
    finish(running).await;
}

#[tokio::test]
async fn a_101_from_the_backend_never_switches_protocols() {
    let running = start(ServerConfig::default()).await;
    let mut stream = connect(&running).await;
    stream
        .write_all(
            b"GET /switch HTTP/1.1\r\nhost: t\r\nconnection: upgrade\r\nupgrade: websocket\r\n\r\n",
        )
        .await
        .unwrap();
    let (head, _) = read_response(&mut stream, true).await;
    assert!(head.starts_with("HTTP/1.1 500"), "{head}");
    finish(running).await;
}

#[tokio::test]
async fn a_client_that_stops_reading_is_closed_by_the_write_timeout() {
    let config = ServerConfig {
        max_connections: 1,
        write_timeout: Duration::from_millis(300),
        ..ServerConfig::default()
    };
    let running = start(config).await;
    let mut stalled = connect(&running).await;
    stalled
        .write_all(b"GET /big HTTP/1.1\r\nhost: t\r\n\r\n")
        .await
        .unwrap();
    // Never read: the socket buffers fill, the write stalls, the slot frees.
    let mut second = connect(&running).await;
    let (head, _) = timeout(WAIT, get(&mut second, "/health")).await.unwrap();
    assert!(head.starts_with("HTTP/1.1 200"), "{head}");
    finish(running).await;
}

async fn status_of(running: &Running, request: Vec<u8>) -> String {
    let mut stream = connect(running).await;
    // The server may answer before it has read the whole request.
    let _ = stream.write_all(&request).await;
    let mut line = Vec::new();
    let mut byte = [0u8; 1];
    while !line.ends_with(b"\r\n") {
        match timeout(WAIT, stream.read(&mut byte)).await {
            Ok(Ok(1)) => line.push(byte[0]),
            _ => break,
        }
    }
    String::from_utf8_lossy(&line).into_owned()
}

#[tokio::test]
async fn hyper_backstops_refuse_oversized_heads() {
    let running = start(ServerConfig::default()).await;
    let big = "a".repeat(60_000);
    let huge = format!("GET /health HTTP/1.1\r\nhost: t\r\nx: {big}\r\n\r\n");
    assert!(status_of(&running, huge.into_bytes()).await.contains("431"));
    let mut many = String::from("GET /health HTTP/1.1\r\nhost: t\r\n");
    for n in 0..150 {
        many.push_str(&format!("x-{n}: v\r\n"));
    }
    many.push_str("\r\n");
    assert!(status_of(&running, many.into_bytes()).await.contains("431"));
    // Over the plan's header limit (32 768) but under hyper's backstop: the
    // backend still sees it, and the plan answers 431 itself.
    let inside = "a".repeat(33_000);
    let request = format!("GET /health HTTP/1.1\r\nhost: t\r\nx: {inside}\r\n\r\n");
    assert!(status_of(&running, request.into_bytes())
        .await
        .contains("200"));
    finish(running).await;
}

#[tokio::test]
async fn stop_lets_a_running_request_finish_and_closes_idle_connections() {
    let running = start(ServerConfig::default()).await;
    let mut busy = connect(&running).await;
    busy.write_all(b"GET /slow HTTP/1.1\r\nhost: t\r\n\r\n")
        .await
        .unwrap();
    timeout(WAIT, running.backend.started.notified())
        .await
        .unwrap();
    let mut idle = connect(&running).await;
    get(&mut idle, "/health").await;

    running.stop.send(true).unwrap();
    assert!(
        is_closed(&mut idle, WAIT).await,
        "idle connection stays open"
    );
    // No new client once the listener is closed.
    let mut refused = false;
    for _ in 0..50 {
        if TcpStream::connect(running.addr).await.is_err() {
            refused = true;
            break;
        }
        sleep(Duration::from_millis(20)).await;
    }
    assert!(refused, "the listener is still open after stop");
    assert!(
        !running.task.is_finished(),
        "serve returned with a request running"
    );

    running.backend.release.notify_one();
    let (head, body) = read_response(&mut busy, true).await;
    assert!(head.starts_with("HTTP/1.1 200"), "{head}");
    assert_eq!(body, b"slow done");
    assert!(is_closed(&mut busy, WAIT).await);
    let closed = timeout(WAIT, running.task).await.unwrap().unwrap();
    assert_eq!(closed, Closed { aborted: 0 });
}

#[tokio::test]
async fn stop_answers_a_connection_accepted_before_it_but_not_yet_read() {
    let running = start(ServerConfig::default()).await;
    let mut waiting = connect(&running).await;
    // Connections are accepted in order: once this one answers, `waiting` is accepted.
    let mut probe = connect(&running).await;
    get(&mut probe, "/health").await;

    running.stop.send(true).unwrap();
    waiting
        .write_all(b"GET /health HTTP/1.1\r\nhost: t\r\n\r\n")
        .await
        .unwrap();
    let (head, _) = read_response(&mut waiting, true).await;
    assert!(head.starts_with("HTTP/1.1 200"), "{head}");
    assert!(is_closed(&mut waiting, WAIT).await);
    let closed = timeout(WAIT, running.task).await.unwrap().unwrap();
    assert_eq!(closed, Closed { aborted: 0 });
}

#[tokio::test]
async fn a_request_that_outlives_the_shutdown_timeout_is_aborted() {
    let config = ServerConfig {
        shutdown_timeout: Duration::from_millis(200),
        ..ServerConfig::default()
    };
    let running = start(config).await;
    let mut stuck = connect(&running).await;
    stuck
        .write_all(b"GET /stuck HTTP/1.1\r\nhost: t\r\n\r\n")
        .await
        .unwrap();
    timeout(WAIT, running.backend.started.notified())
        .await
        .unwrap();
    assert_eq!(finish(running).await, Closed { aborted: 1 });
    assert!(is_closed(&mut stuck, WAIT).await);
}

#[tokio::test]
async fn a_dropped_stop_sender_stops_the_server() {
    let running = start(ServerConfig::default()).await;
    let Running { task, stop, .. } = running;
    drop(stop);
    assert_eq!(
        timeout(WAIT, task).await.unwrap().unwrap(),
        Closed { aborted: 0 }
    );
}

#[tokio::test]
async fn a_client_that_shuts_down_its_write_side_still_gets_the_answer() {
    let running = start(ServerConfig::default()).await;
    let mut stream = connect(&running).await;
    stream
        .write_all(b"GET /slow HTTP/1.1\r\nhost: t\r\n\r\n")
        .await
        .unwrap();
    stream.shutdown().await.unwrap();
    timeout(WAIT, running.backend.started.notified())
        .await
        .unwrap();
    sleep(Duration::from_millis(100)).await; // the server has seen the EOF
    running.backend.release.notify_one();
    let (head, body) = read_response(&mut stream, true).await;
    assert!(head.starts_with("HTTP/1.1 200"), "{head}");
    assert_eq!(body, b"slow done");
    finish(running).await;
}

#[tokio::test]
async fn http_1_1_needs_exactly_one_host() {
    let running = start(ServerConfig::default()).await;
    for (request, expected) in [
        ("GET /x HTTP/1.1\r\n\r\n", "HTTP/1.1 400"),
        (
            "GET /x HTTP/1.1\r\nhost: a\r\nhost: b\r\n\r\n",
            "HTTP/1.1 400",
        ),
        ("GET /x HTTP/1.1\r\nhost: a\r\n\r\n", "HTTP/1.1 200"),
        ("GET /x HTTP/1.0\r\n\r\n", "HTTP/1.0 200"),
        (
            "GET /x HTTP/1.0\r\nhost: a\r\nhost: b\r\n\r\n",
            "HTTP/1.0 400",
        ),
    ] {
        let mut stream = connect(&running).await;
        stream.write_all(request.as_bytes()).await.unwrap();
        let (head, _) = read_response(&mut stream, true).await;
        assert!(head.starts_with(expected), "{request:?} -> {head}");
    }
    finish(running).await;
}

#[tokio::test]
async fn an_absolute_form_target_makes_its_authority_the_host() {
    let running = start(ServerConfig::default()).await;
    let mut stream = connect(&running).await;
    stream
        .write_all(b"GET http://evil.example/echo HTTP/1.1\r\nhost: other\r\n\r\n")
        .await
        .unwrap();
    let (_, body) = read_response(&mut stream, true).await;
    let text = String::from_utf8(body).unwrap();
    assert!(text.starts_with("GET /echo "), "{text}");
    assert!(text.ends_with("host=evil.example"), "{text}");
    finish(running).await;
}

#[tokio::test]
async fn an_unbounded_header_limit_does_not_overflow() {
    let config = ServerConfig {
        max_header_bytes: usize::MAX,
        ..ServerConfig::default()
    };
    let running = start(config).await;
    let mut stream = connect(&running).await;
    let (head, _) = get(&mut stream, "/ok").await;
    assert!(head.starts_with("HTTP/1.1 200"), "{head}");
    finish(running).await;
}
