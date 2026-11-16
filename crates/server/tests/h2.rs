//! HTTP/2 over real TLS loopback sockets: ALPN, requests through a real `h2`
//! client, the stream and reset limits through hand-written frames, flow
//! control and a graceful stop.

use std::future::poll_fn;
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;

use bytes::Bytes;
use h2::client::{self, SendRequest};
use hinoki_server::tls::acceptor;
use hinoki_server::{serve, Answer, Backend, Closed, Incoming, ServerConfig};
use http::Request;
use rustls::crypto::ring::default_provider;
use rustls::pki_types::pem::PemObject;
use rustls::pki_types::{CertificateDer, ServerName};
use rustls::{ClientConfig, RootCertStore};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{watch, Notify};
use tokio::task::JoinHandle;
use tokio::time::{sleep, timeout};
use tokio_rustls::client::TlsStream;
use tokio_rustls::TlsConnector;

const WAIT: Duration = Duration::from_secs(10);
const MEBIBYTE: usize = 1024 * 1024;
const PREFACE: &[u8] = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n";

#[derive(Default)]
struct Fake {
    /// Requests that reached the backend, by path prefix of the handler.
    started: AtomicUsize,
    release: Notify,
}

fn answer(body: &[u8]) -> Answer {
    Answer {
        status: 200,
        headers: vec![
            (b"content-type".to_vec(), b"text/plain".to_vec()),
            (
                b"content-length".to_vec(),
                body.len().to_string().into_bytes(),
            ),
        ],
        body: body.to_vec(),
    }
}

fn hosts_of(request: &Incoming) -> String {
    let hosts: Vec<String> = request
        .headers
        .iter()
        .filter(|(name, _)| name == b"host")
        .map(|(_, value)| String::from_utf8_lossy(value).into_owned())
        .collect();
    hosts.join(",")
}

impl Backend for Fake {
    async fn handle(&self, mut request: Incoming) -> Answer {
        self.started.fetch_add(1, Ordering::SeqCst);
        match request.path.as_str() {
            "/info" => answer(
                format!(
                    "method={} version={} secure={} hosts={} refusal={}",
                    request.method,
                    request.http_version,
                    request.secure,
                    hosts_of(&request),
                    request.refusal.is_some()
                )
                .as_bytes(),
            ),
            "/echo" => {
                let body = match request.body.take() {
                    Some(reader) => reader.read_all(MEBIBYTE).await.unwrap(),
                    None => Vec::new(),
                };
                answer(&body)
            }
            "/up" => {
                let body = request.body.take().unwrap();
                let size = body.read_all(8 * MEBIBYTE).await.unwrap().len();
                answer(format!("size={size}").as_bytes())
            }
            "/big" => answer(&vec![b'x'; 5 * MEBIBYTE]),
            "/slow" => {
                self.release.notified().await;
                answer(b"slow done")
            }
            "/hold" => std::future::pending().await,
            _ => answer(b"hello"),
        }
    }
}

fn fixture(name: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/tls")
        .join(name)
}

struct Running {
    addr: SocketAddr,
    backend: Arc<Fake>,
    stop: watch::Sender<bool>,
    task: JoinHandle<Closed>,
}

async fn start(config: ServerConfig, http2: bool) -> Running {
    let tls = acceptor(
        &fixture("localhost-cert.pem"),
        &fixture("localhost-key.pem"),
        http2,
    )
    .unwrap();
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let backend = Arc::new(Fake::default());
    let (stop, receiver) = watch::channel(false);
    let task = tokio::spawn(serve(
        listener,
        Arc::clone(&backend),
        config,
        Some(tls),
        receiver,
    ));
    Running {
        addr,
        backend,
        stop,
        task,
    }
}

fn quick() -> ServerConfig {
    ServerConfig {
        shutdown_timeout: Duration::from_millis(500),
        ..ServerConfig::default()
    }
}

fn connector(alpn: &[&[u8]]) -> TlsConnector {
    let mut roots = RootCertStore::empty();
    for cert in CertificateDer::pem_file_iter(fixture("localhost-cert.pem")).unwrap() {
        roots.add(cert.unwrap()).unwrap();
    }
    let mut config = ClientConfig::builder_with_provider(Arc::new(default_provider()))
        .with_safe_default_protocol_versions()
        .unwrap()
        .with_root_certificates(roots)
        .with_no_client_auth();
    config.alpn_protocols = alpn.iter().map(|p| p.to_vec()).collect();
    TlsConnector::from(Arc::new(config))
}

async fn secure(running: &Running, alpn: &[&[u8]]) -> TlsStream<TcpStream> {
    let tcp = TcpStream::connect(running.addr).await.unwrap();
    connector(alpn)
        .connect(ServerName::try_from("localhost").unwrap(), tcp)
        .await
        .unwrap()
}

type Driver = JoinHandle<Result<(), h2::Error>>;

async fn h2_client(running: &Running) -> (SendRequest<Bytes>, Driver) {
    let stream = secure(running, &[b"h2", b"http/1.1"]).await;
    assert_eq!(stream.get_ref().1.alpn_protocol(), Some(&b"h2"[..]));
    let (send, connection) = client::handshake(stream).await.unwrap();
    (send, tokio::spawn(connection))
}

fn request(method: &str, path: &str) -> http::request::Builder {
    Request::builder()
        .method(method)
        .uri(format!("https://localhost{path}"))
}

async fn send_all(stream: &mut h2::SendStream<Bytes>, mut data: Bytes) {
    while !data.is_empty() {
        stream.reserve_capacity(data.len());
        let granted = poll_fn(|cx| stream.poll_capacity(cx))
            .await
            .unwrap()
            .unwrap();
        let chunk = data.split_to(granted.min(data.len()));
        stream.send_data(chunk, data.is_empty()).unwrap();
    }
}

async fn read_all(mut body: h2::RecvStream) -> Vec<u8> {
    let mut out = Vec::new();
    while let Some(chunk) = body.data().await {
        let chunk = chunk.unwrap();
        body.flow_control().release_capacity(chunk.len()).unwrap();
        out.extend_from_slice(&chunk);
    }
    out
}

/// Send one request and read the whole answer.
async fn call(
    send: &SendRequest<Bytes>,
    request: Request<()>,
    body: Option<Bytes>,
) -> (http::response::Parts, Vec<u8>) {
    let mut ready = send.clone().ready().await.unwrap();
    let (response, mut stream) = ready.send_request(request, body.is_none()).unwrap();
    if let Some(data) = body {
        send_all(&mut stream, data).await;
    }
    let (parts, incoming) = timeout(WAIT, response).await.unwrap().unwrap().into_parts();
    let bytes = timeout(WAIT, read_all(incoming)).await.unwrap();
    (parts, bytes)
}

// ---- hand-written frames, for what a well-behaved client never sends ----

fn frame(kind: u8, flags: u8, stream: u32, payload: &[u8]) -> Vec<u8> {
    let len = u32::try_from(payload.len()).unwrap();
    let mut out = vec![(len >> 16) as u8, (len >> 8) as u8, len as u8, kind, flags];
    out.extend(stream.to_be_bytes());
    out.extend(payload);
    out
}

/// GET `path` on `localhost`, as one HPACK block (indexed `:method GET`,
/// `:scheme https`, literal `:path` and `:authority`), END_STREAM | END_HEADERS.
fn get_frame(stream: u32, path: &str) -> Vec<u8> {
    let mut block = vec![0x82, 0x87, 0x04, u8::try_from(path.len()).unwrap()];
    block.extend(path.as_bytes());
    block.extend([0x01, 9]);
    block.extend(b"localhost");
    frame(1, 5, stream, &block)
}

fn reset_frame(stream: u32) -> Vec<u8> {
    frame(3, 0, stream, &8u32.to_be_bytes())
}

type Frame = (u8, u8, u32, Vec<u8>);

async fn next_frame(stream: &mut TlsStream<TcpStream>) -> Option<Frame> {
    let mut head = [0u8; 9];
    stream.read_exact(&mut head).await.ok()?;
    let len = (usize::from(head[0]) << 16) | (usize::from(head[1]) << 8) | usize::from(head[2]);
    let id = u32::from_be_bytes([head[5], head[6], head[7], head[8]]) & 0x7fff_ffff;
    let mut payload = vec![0u8; len];
    stream.read_exact(&mut payload).await.ok()?;
    Some((head[3], head[4], id, payload))
}

/// The preface and an empty SETTINGS, then the server's SETTINGS acknowledged.
async fn raw_h2(running: &Running) -> TlsStream<TcpStream> {
    let mut stream = secure(running, &[b"h2"]).await;
    stream.write_all(PREFACE).await.unwrap();
    stream.write_all(&frame(4, 0, 0, &[])).await.unwrap();
    loop {
        let (kind, flags, _, _) = timeout(WAIT, next_frame(&mut stream))
            .await
            .unwrap()
            .expect("server settings");
        if kind == 4 && flags & 1 == 0 {
            break;
        }
    }
    stream.write_all(&frame(4, 1, 0, &[])).await.unwrap();
    stream
}

// ---- tests ----

#[tokio::test]
async fn alpn_picks_h2_only_when_it_is_on_and_offered() {
    let on = start(quick(), true).await;
    let both: [&[u8]; 2] = [b"h2", b"http/1.1"];
    let h1_only: [&[u8]; 1] = [b"http/1.1"];
    let none: [&[u8]; 0] = [];
    for (alpn, expected) in [
        (&both[..], Some(&b"h2"[..])),
        (&h1_only[..], Some(&b"http/1.1"[..])),
        (&none[..], None),
    ] {
        let stream = secure(&on, alpn).await;
        assert_eq!(stream.get_ref().1.alpn_protocol(), expected);
    }
    on.stop.send(true).unwrap();
    let off = start(quick(), false).await;
    let stream = secure(&off, &both).await;
    assert_eq!(stream.get_ref().1.alpn_protocol(), Some(&b"http/1.1"[..]));
    off.stop.send(true).unwrap();
}

#[tokio::test]
async fn http1_over_tls_still_works_with_http2_on() {
    let running = start(quick(), true).await;
    let h1_only: [&[u8]; 1] = [b"http/1.1"];
    let none: [&[u8]; 0] = [];
    for alpn in [&h1_only[..], &none[..]] {
        let mut stream = secure(&running, alpn).await;
        stream
            .write_all(b"GET /info HTTP/1.1\r\nhost: localhost\r\nconnection: close\r\n\r\n")
            .await
            .unwrap();
        let mut raw = Vec::new();
        timeout(WAIT, stream.read_to_end(&mut raw))
            .await
            .unwrap()
            .ok();
        let text = String::from_utf8(raw).unwrap();
        assert!(text.starts_with("HTTP/1.1 200"), "{text}");
        assert!(text.contains("version=1.1 secure=true"), "{text}");
    }
    running.stop.send(true).unwrap();
}

#[tokio::test]
async fn get_head_and_post_work_over_h2() {
    let running = start(quick(), true).await;
    let (send, _driver) = h2_client(&running).await;
    let (parts, body) = call(&send, request("GET", "/info").body(()).unwrap(), None).await;
    assert_eq!(parts.status, 200);
    assert_eq!(parts.headers["content-type"], "text/plain");
    let text = String::from_utf8(body).unwrap();
    assert!(
        text.starts_with("method=GET version=2 secure=true hosts=localhost"),
        "{text}"
    );
    let (parts, body) = call(&send, request("HEAD", "/info").body(()).unwrap(), None).await;
    assert_eq!(parts.status, 200);
    assert!(
        parts.headers["content-length"]
            .to_str()
            .unwrap()
            .parse::<usize>()
            .unwrap()
            > 0
    );
    assert!(body.is_empty());
    let post = request("POST", "/echo").body(()).unwrap();
    let (parts, body) = call(&send, post, Some(Bytes::from_static(b"payload"))).await;
    assert_eq!(
        (parts.status.as_u16(), body.as_slice()),
        (200, &b"payload"[..])
    );
    running.stop.send(true).unwrap();
}

#[tokio::test]
async fn authority_wins_over_host_and_no_connection_header_is_sent() {
    let running = start(quick(), true).await;
    let (send, _driver) = h2_client(&running).await;
    let mismatched = Request::builder()
        .uri("https://authority.example/info")
        .header("host", "other.example")
        .body(())
        .unwrap();
    let (_, body) = call(&send, mismatched, None).await;
    let text = String::from_utf8(body).unwrap();
    assert!(
        text.contains("hosts=authority.example refusal=false"),
        "{text}"
    );
    // An unread request body makes an HTTP/1 connection close; on h2 the
    // forbidden `connection` header must not appear.
    let unread = request("POST", "/info").body(()).unwrap();
    let (parts, _) = call(&send, unread, Some(Bytes::from_static(b"unread"))).await;
    assert_eq!(parts.status, 200);
    assert!(
        !parts.headers.contains_key("connection"),
        "{:?}",
        parts.headers
    );
    running.stop.send(true).unwrap();
}

#[tokio::test]
async fn a_rapid_reset_client_is_cut_off_and_the_server_keeps_serving() {
    let running = start(quick(), true).await;
    let mut raw = raw_h2(&running).await;
    let mut burst = Vec::new();
    for n in 0..2000u32 {
        burst.extend(get_frame(2 * n + 1, "/slow"));
        burst.extend(reset_frame(2 * n + 1));
    }
    let _ = raw.write_all(&burst).await;
    let cut = timeout(WAIT, async {
        while let Some((kind, ..)) = next_frame(&mut raw).await {
            if kind == 7 {
                return "goaway";
            }
        }
        "closed"
    })
    .await;
    eprintln!("rapid reset ended with: {cut:?}");
    assert!(cut.is_ok(), "the connection was not cut off in time");
    let (send, _driver) = h2_client(&running).await;
    let (parts, _) = call(&send, request("GET", "/info").body(()).unwrap(), None).await;
    assert_eq!(parts.status, 200);
    running.stop.send(true).unwrap();
}

#[tokio::test]
async fn the_101st_concurrent_stream_is_refused() {
    let running = start(quick(), true).await;
    let mut raw = raw_h2(&running).await;
    for n in 0..101u32 {
        raw.write_all(&get_frame(2 * n + 1, "/hold")).await.unwrap();
    }
    let refused = timeout(WAIT, async {
        while let Some((kind, _, id, payload)) = next_frame(&mut raw).await {
            if kind == 3 {
                return Some((id, payload));
            }
        }
        None
    })
    .await
    .unwrap()
    .expect("no stream was refused");
    assert_eq!(refused.0, 201, "the refused stream");
    assert_eq!(refused.1, 7u32.to_be_bytes(), "REFUSED_STREAM");
    timeout(WAIT, async {
        while running.backend.started.load(Ordering::SeqCst) < 100 {
            sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .expect("the 100 streams did not all reach the backend");
    sleep(Duration::from_millis(100)).await;
    assert_eq!(running.backend.started.load(Ordering::SeqCst), 100);
    running.stop.send(true).unwrap();
}

#[tokio::test]
async fn an_oversized_header_list_is_refused() {
    let config = ServerConfig {
        max_header_bytes: 8 * 1024,
        ..quick()
    };
    let running = start(config, true).await;
    let (send, _driver) = h2_client(&running).await;
    let big = request("GET", "/info")
        .header("x-big", "a".repeat(12 * 1024))
        .body(())
        .unwrap();
    let mut ready = send.clone().ready().await.unwrap();
    let outcome = match ready.send_request(big, true) {
        Ok((response, _)) => timeout(WAIT, response).await.unwrap().map(|r| r.status()),
        Err(error) => Err(error),
    };
    match outcome {
        Ok(status) => assert_eq!(status, 431),
        Err(error) => eprintln!("oversized headers refused with: {error}"),
    }
    assert_eq!(running.backend.started.load(Ordering::SeqCst), 0);
    running.stop.send(true).unwrap();
}

#[tokio::test]
async fn a_large_body_flows_both_ways_under_flow_control() {
    let running = start(quick(), true).await;
    let (send, _driver) = h2_client(&running).await;
    let upload = request("POST", "/up").body(()).unwrap();
    let data = Bytes::from(vec![b'u'; 5 * MEBIBYTE]);
    let (_, body) = call(&send, upload, Some(data)).await;
    assert_eq!(body, format!("size={}", 5 * MEBIBYTE).into_bytes());
    let (parts, body) = call(&send, request("GET", "/big").body(()).unwrap(), None).await;
    assert_eq!(parts.status, 200);
    assert_eq!(body.len(), 5 * MEBIBYTE);
    running.stop.send(true).unwrap();
}

#[tokio::test]
async fn a_stop_sends_goaway_finishes_the_stream_in_flight_and_refuses_new_connections() {
    let running = start(
        ServerConfig {
            shutdown_timeout: Duration::from_secs(5),
            ..ServerConfig::default()
        },
        true,
    )
    .await;
    let (send, driver) = h2_client(&running).await;
    let mut slow = send.clone().ready().await.unwrap();
    let (response, _) = slow
        .send_request(request("GET", "/slow").body(()).unwrap(), true)
        .unwrap();
    while running.backend.started.load(Ordering::SeqCst) == 0 {
        sleep(Duration::from_millis(10)).await;
    }
    running.stop.send(true).unwrap();
    // GOAWAY: no further request is accepted on the connection.
    let refused = timeout(WAIT, async {
        loop {
            match send.clone().ready().await {
                Err(_) => return,
                Ok(ready) => drop(ready),
            }
            sleep(Duration::from_millis(20)).await;
        }
    })
    .await;
    assert!(refused.is_ok(), "no GOAWAY reached the client");
    // The listener is gone.
    let gone = timeout(WAIT, async {
        while TcpStream::connect(running.addr).await.is_ok() {
            sleep(Duration::from_millis(20)).await;
        }
    })
    .await;
    assert!(gone.is_ok(), "new connections are still accepted");
    // The stream in flight completes.
    running.backend.release.notify_one();
    let (parts, incoming) = timeout(WAIT, response).await.unwrap().unwrap().into_parts();
    assert_eq!(parts.status, 200);
    assert_eq!(read_all(incoming).await, b"slow done");
    let closed = timeout(WAIT, running.task).await.unwrap().unwrap();
    assert_eq!(closed, Closed { aborted: 0 });
    let _ = timeout(WAIT, driver).await.unwrap();
}

#[tokio::test]
async fn a_connection_that_opens_no_stream_is_closed() {
    let config = ServerConfig {
        header_read_timeout: Duration::from_millis(300),
        ..quick()
    };
    let running = start(config, true).await;
    let (_send, driver) = h2_client(&running).await;
    let ended = timeout(Duration::from_secs(3), driver).await;
    assert!(ended.is_ok(), "an idle h2 connection was kept open");
    running.stop.send(true).unwrap();
}
