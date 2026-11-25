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
use http::Request;
use kiso::tls::acceptor;
use kiso::{
    serve, Aborted, Answer, Backend, Closed, Delivery, FileAnswer, FileBody, Incoming, NativeJobs,
    Pace, ServerConfig, Streamed,
};
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

struct Fake {
    /// Requests that reached the backend, by path prefix of the handler.
    started: AtomicUsize,
    release: Notify,
    native_jobs: Arc<NativeJobs>,
    native_started: Arc<Notify>,
    native_release: Arc<Notify>,
    native_handler_cancelled: Arc<Notify>,
    producer_gone: Arc<Notify>,
}

impl Default for Fake {
    fn default() -> Self {
        Self {
            started: AtomicUsize::new(0),
            release: Notify::new(),
            native_jobs: Arc::new(NativeJobs::new(1)),
            native_started: Arc::new(Notify::new()),
            native_release: Arc::new(Notify::new()),
            native_handler_cancelled: Arc::new(Notify::new()),
            producer_gone: Arc::new(Notify::new()),
        }
    }
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
        body: Bytes::copy_from_slice(body),
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
        if let Some(early) = &request.refusal {
            return Answer {
                status: early.status(),
                ..answer(b"")
            };
        }
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
            "/native-stub" => {
                let (mut sender, receiver) = tokio::sync::oneshot::channel();
                let started = Arc::clone(&self.native_started);
                let release = Arc::clone(&self.native_release);
                let handler_cancelled = Arc::clone(&self.native_handler_cancelled);
                if !self
                    .native_jobs
                    .spawn(async move {
                        started.notify_one();
                        let mut cancelled = false;
                        tokio::select! {
                            _ = release.notified() => {}
                            _ = sender.closed() => {
                                handler_cancelled.notify_one();
                                cancelled = true;
                            }
                        }
                        if cancelled {
                            release.notified().await;
                        }
                        let _ = sender.send(());
                    })
                    .await
                {
                    return Answer {
                        status: 503,
                        ..answer(b"overloaded")
                    };
                }
                let _ = receiver.await;
                answer(b"native stub done")
            }
            "/hold" => std::future::pending().await,
            _ => answer(b"hello"),
        }
    }

    async fn deliver(&self, request: Incoming) -> Delivery {
        match request.path.as_str() {
            "/live" => Self::live(),
            "/sse" => Self::sse(),
            "/abort" => Self::abort(),
            "/gone" => self.gone(),
            "/file" => Self::file().await,
            _ => Delivery::Whole(self.handle(request).await),
        }
    }
}

impl Fake {
    fn streamed(chunks: tokio::sync::mpsc::Receiver<kiso::Chunk>, pace: Pace) -> Delivery {
        Delivery::Streamed(Streamed {
            status: 200,
            headers: Vec::new(),
            chunks,
            pace,
        })
    }

    fn live() -> Delivery {
        let (sender, chunks) = tokio::sync::mpsc::channel(1);
        tokio::spawn(async move {
            sender.send(Ok(Bytes::from_static(b"one "))).await.ok();
            sleep(Duration::from_millis(1500)).await;
            sender.send(Ok(Bytes::from_static(b"two"))).await.ok();
        });
        Self::streamed(chunks, Pace::default())
    }

    /// Silent for 400 ms with a 100 ms keep-alive, then one event and the end.
    fn sse() -> Delivery {
        let (sender, chunks) = tokio::sync::mpsc::channel(1);
        tokio::spawn(async move {
            sleep(Duration::from_millis(400)).await;
            sender
                .send(Ok(Bytes::from_static(b"data: x\n\n")))
                .await
                .ok();
        });
        let pace = Pace {
            keepalive: Some(Duration::from_millis(100)),
            ..Pace::default()
        };
        Self::streamed(chunks, pace)
    }

    fn abort() -> Delivery {
        let (sender, chunks) = tokio::sync::mpsc::channel(2);
        tokio::spawn(async move {
            sender.send(Ok(Bytes::from_static(b"partial"))).await.ok();
            sender.send(Err(Aborted)).await.ok();
        });
        Self::streamed(chunks, Pace::default())
    }

    /// A producer that never ends and says when its receiver is dropped.
    fn gone(&self) -> Delivery {
        let (sender, chunks) = tokio::sync::mpsc::channel(1);
        let gone = Arc::clone(&self.producer_gone);
        tokio::spawn(async move {
            sender.send(Ok(Bytes::from_static(b"first"))).await.ok();
            sender.closed().await;
            gone.notify_one();
        });
        Self::streamed(chunks, Pace::default())
    }

    async fn file() -> Delivery {
        let file = FileBody::open(&big_file()).await.unwrap();
        let headers = vec![(
            b"content-length".to_vec(),
            file.len().to_string().into_bytes(),
        )];
        Delivery::File(FileAnswer {
            status: 200,
            headers,
            file,
        })
    }
}

const BIG_FILE_LEN: usize = 200_000;

/// A file larger than several HTTP/2 frames and file pieces, with bytes that
/// differ along it so a reordered or repeated piece is noticed.
fn big_file() -> PathBuf {
    let path = Path::new(env!("CARGO_TARGET_TMPDIR")).join("h2-big-file.bin");
    let bytes: Vec<u8> = (0..BIG_FILE_LEN).map(|n| (n % 251) as u8).collect();
    std::fs::write(&path, bytes).unwrap();
    path
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
    tcp.set_nodelay(true).unwrap();
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

/// GET `path` with no `:authority` and these literal headers (names and values
/// under 127 bytes), END_STREAM | END_HEADERS.
fn get_without_authority(stream: u32, path: &str, extra: &[(&str, &str)]) -> Vec<u8> {
    let mut block = vec![0x82, 0x87, 0x04, u8::try_from(path.len()).unwrap()];
    block.extend(path.as_bytes());
    for (name, value) in extra {
        block.push(0x00);
        block.push(u8::try_from(name.len()).unwrap());
        block.extend(name.as_bytes());
        block.push(u8::try_from(value.len()).unwrap());
        block.extend(value.as_bytes());
    }
    frame(1, 5, stream, &block)
}

fn reset_frame(stream: u32) -> Vec<u8> {
    frame(3, 0, stream, &8u32.to_be_bytes())
}

type Frame = (u8, u8, u32, Vec<u8>);

async fn next_frame<R: AsyncReadExt + Unpin>(stream: &mut R) -> Option<Frame> {
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
    raw_h2_with(running, &[]).await
}

/// As [`raw_h2`], with these SETTINGS payload entries.
async fn raw_h2_with(running: &Running, settings: &[u8]) -> TlsStream<TcpStream> {
    let mut stream = secure(running, &[b"h2"]).await;
    stream.write_all(PREFACE).await.unwrap();
    stream.write_all(&frame(4, 0, 0, settings)).await.unwrap();
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
async fn the_33rd_concurrent_stream_is_refused() {
    let running = start(quick(), true).await;
    let mut raw = raw_h2(&running).await;
    for n in 0..33u32 {
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
    assert_eq!(refused.0, 65, "the refused stream");
    assert_eq!(refused.1, 7u32.to_be_bytes(), "REFUSED_STREAM");
    timeout(WAIT, async {
        while running.backend.started.load(Ordering::SeqCst) < 32 {
            sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .expect("the 32 streams did not all reach the backend");
    sleep(Duration::from_millis(100)).await;
    assert_eq!(running.backend.started.load(Ordering::SeqCst), 32);
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
async fn an_in_flight_http2_stub_job_survives_client_reset_until_shutdown_drains_it() {
    let running = start(quick(), true).await;
    let (send, driver) = h2_client(&running).await;
    let mut ready = send.clone().ready().await.unwrap();
    let (response, stream) = ready
        .send_request(request("GET", "/native-stub").body(()).unwrap(), true)
        .unwrap();
    timeout(WAIT, running.backend.native_started.notified())
        .await
        .expect("stub job did not start");
    assert_eq!(running.backend.native_jobs.in_flight(), 1);

    drop(response);
    drop(stream);
    timeout(WAIT, running.backend.native_handler_cancelled.notified())
        .await
        .expect("server handler did not observe the client reset");
    running.stop.send(true).unwrap();
    timeout(WAIT, running.task)
        .await
        .expect("HTTP/2 server did not stop")
        .unwrap();
    assert_eq!(
        running
            .backend
            .native_jobs
            .drain(Duration::from_millis(1))
            .await,
        1
    );
    assert_eq!(running.backend.native_jobs.in_flight(), 0);
    drop(driver);
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

/// Reads frames until the server closes the connection or `bound` passes.
async fn closed_within(stream: &mut TlsStream<TcpStream>, bound: Duration) -> bool {
    timeout(bound, async { while next_frame(stream).await.is_some() {} })
        .await
        .is_ok()
}

fn config_with_timeouts() -> ServerConfig {
    ServerConfig {
        header_read_timeout: Duration::from_millis(300),
        write_timeout: Duration::from_millis(300),
        ..quick()
    }
}

#[tokio::test]
async fn a_connection_that_goes_idle_after_a_stream_is_closed() {
    let running = start(config_with_timeouts(), true).await;
    let (send, driver) = h2_client(&running).await;
    let (parts, _) = call(&send, request("GET", "/info").body(()).unwrap(), None).await;
    assert_eq!(parts.status, 200);
    let ended = timeout(Duration::from_secs(3), driver).await;
    assert!(ended.is_ok(), "an idle h2 connection kept its slot");
    drop(send);
    running.stop.send(true).unwrap();
}

#[tokio::test]
async fn a_client_that_never_opens_its_window_is_closed_after_the_write_timeout() {
    let running = start(config_with_timeouts(), true).await;
    // SETTINGS_INITIAL_WINDOW_SIZE = 0: no DATA may ever be sent.
    let mut raw = raw_h2_with(&running, &[0, 4, 0, 0, 0, 0]).await;
    raw.write_all(&get_frame(1, "/big")).await.unwrap();
    assert!(
        closed_within(&mut raw, Duration::from_secs(3)).await,
        "a stalled response held the connection"
    );
    running.stop.send(true).unwrap();
}

#[tokio::test]
async fn paced_resets_of_accepted_streams_get_the_connection_dropped() {
    let running = start(quick(), true).await;
    let mut raw = raw_h2(&running).await;
    for n in 0..400u32 {
        let pair = [get_frame(2 * n + 1, "/slow"), reset_frame(2 * n + 1)].concat();
        if raw.write_all(&pair).await.is_err() {
            break;
        }
        sleep(Duration::from_millis(2)).await;
    }
    assert!(
        closed_within(&mut raw, WAIT).await,
        "the reset flood went on"
    );
    assert!(running.backend.started.load(Ordering::SeqCst) < 200);
    let (send, _driver) = h2_client(&running).await;
    let (parts, _) = call(&send, request("GET", "/info").body(()).unwrap(), None).await;
    assert_eq!(parts.status, 200);
    running.stop.send(true).unwrap();
}

#[tokio::test]
async fn several_host_headers_without_authority_are_refused() {
    let running = start(quick(), true).await;
    let mut raw = raw_h2(&running).await;
    let two = [("host", "a.example"), ("host", "b.example")];
    raw.write_all(&get_without_authority(1, "/info", &two))
        .await
        .unwrap();
    raw.write_all(&get_without_authority(3, "/info", &two[..1]))
        .await
        .unwrap();
    let mut statuses = Vec::new();
    while statuses.len() < 2 {
        let (kind, _, id, payload) = timeout(WAIT, next_frame(&mut raw))
            .await
            .unwrap()
            .expect("an answer");
        // The first HPACK byte of a response head: 0x88 is `:status 200`,
        // anything else is another status (400 is a literal).
        if kind == 1 {
            statuses.push((id, payload[0]));
        }
    }
    statuses.sort_unstable();
    assert_eq!(statuses[1], (3, 0x88), "one host header is served");
    assert_ne!(statuses[0].1, 0x88, "two host headers are not");
    running.stop.send(true).unwrap();
}

#[tokio::test]
async fn more_headers_than_the_limit_are_refused_with_431() {
    let running = start(quick(), true).await;
    let (send, _driver) = h2_client(&running).await;
    let mut many = request("GET", "/info");
    for n in 0..600 {
        many = many.header(format!("x-n{n}"), "v");
    }
    let (parts, _) = call(&send, many.body(()).unwrap(), None).await;
    assert_eq!(parts.status, 431);
    let (parts, _) = call(&send, request("GET", "/info").body(()).unwrap(), None).await;
    assert_eq!(parts.status, 200);
    running.stop.send(true).unwrap();
}

#[tokio::test]
async fn cookie_crumbs_count_as_one_header() {
    let running = start(quick(), true).await;
    let (send, _driver) = h2_client(&running).await;
    let mut crumbs = request("GET", "/info");
    for n in 0..20 {
        crumbs = crumbs.header(format!("x-n{n}"), "v");
    }
    for n in 0..90 {
        crumbs = crumbs.header("cookie", format!("c{n}=v"));
    }
    let (parts, _) = call(&send, crumbs.body(()).unwrap(), None).await;
    assert_eq!(parts.status, 200);
    running.stop.send(true).unwrap();
}

fn stall_config() -> ServerConfig {
    ServerConfig {
        write_timeout: Duration::from_millis(300),
        shutdown_timeout: Duration::from_secs(3),
        ..ServerConfig::default()
    }
}

#[tokio::test]
async fn new_streams_do_not_hide_a_stalled_response() {
    let running = start(stall_config(), true).await;
    let mut raw = raw_h2_with(&running, &[0, 4, 0, 0, 0, 0]).await;
    raw.write_all(&get_frame(1, "/big")).await.unwrap();
    let (mut reader, mut writer) = tokio::io::split(raw);
    let flood = async {
        for n in 1..200u32 {
            let pair = [get_frame(2 * n + 1, "/big"), reset_frame(2 * n + 1)].concat();
            if writer.write_all(&pair).await.is_err() {
                return;
            }
            sleep(Duration::from_millis(100)).await;
        }
    };
    let closed = timeout(Duration::from_secs(6), async {
        tokio::select! {
            () = async { while next_frame(&mut reader).await.is_some() {} } => {}
            () = flood => {}
        }
    })
    .await;
    assert!(
        closed.is_ok(),
        "fresh streams kept a stalled response alive"
    );
    running.stop.send(true).unwrap();
}

#[tokio::test]
async fn a_stalled_response_does_not_cut_a_slow_stream_on_the_connection() {
    let running = start(stall_config(), true).await;
    let stream = secure(&running, &[b"h2"]).await;
    let (send, connection) = client::Builder::new()
        .initial_window_size(1024)
        .handshake::<_, Bytes>(stream)
        .await
        .unwrap();
    let driver = tokio::spawn(connection);
    // Never read: stuck after the first kibibyte.
    let mut stalled = send.clone().ready().await.unwrap();
    let (_unread, _) = stalled
        .send_request(request("GET", "/big").body(()).unwrap(), true)
        .unwrap();
    let mut slow = send.clone().ready().await.unwrap();
    let (answer, _) = slow
        .send_request(request("GET", "/slow").body(()).unwrap(), true)
        .unwrap();
    // The stall is detected (300 ms) while /slow is still being computed: the
    // client is told to open nothing new.
    let told = timeout(Duration::from_secs(2), async {
        while send.clone().ready().await.is_ok() {
            sleep(Duration::from_millis(20)).await;
        }
    })
    .await;
    assert!(told.is_ok(), "the stall never produced a GOAWAY");
    running.backend.release.notify_one();
    let (parts, body) = timeout(WAIT, answer).await.unwrap().unwrap().into_parts();
    assert_eq!(parts.status, 200);
    assert_eq!(read_all(body).await, b"slow done");
    // The stalled stream is cut with the connection at the shutdown timeout.
    let ended = timeout(WAIT, driver).await;
    assert!(
        ended.is_ok(),
        "the connection outlived its shutdown timeout"
    );
    running.stop.send(true).unwrap();
}

#[tokio::test]
async fn a_slow_producer_is_not_a_stalled_client() {
    // The producer pauses for 1.5 s against a 300 ms write timeout and a
    // 400 ms shutdown timeout, so a stall verdict would cut the response.
    let config = ServerConfig {
        shutdown_timeout: Duration::from_millis(400),
        ..stall_config()
    };
    let running = start(config, true).await;
    let (send, _driver) = h2_client(&running).await;
    let (parts, body) = call(&send, request("GET", "/live").body(()).unwrap(), None).await;
    assert_eq!(parts.status, 200);
    assert_eq!(body, b"one two");
    running.stop.send(true).unwrap();
}

#[tokio::test]
async fn an_event_stream_keeps_alive_while_the_producer_is_silent() {
    let running = start(quick(), true).await;
    let (send, _driver) = h2_client(&running).await;
    let (parts, body) = call(&send, request("GET", "/sse").body(()).unwrap(), None).await;
    assert_eq!(parts.status, 200);
    let text = String::from_utf8(body).unwrap();
    assert!(text.ends_with("data: x\n\n"), "{text:?}");
    let beats = text.matches(": \n\n").count();
    assert!((2..=4).contains(&beats), "{beats} keep-alives in {text:?}");
    running.stop.send(true).unwrap();
}

#[tokio::test]
async fn an_aborted_producer_resets_the_stream_and_leaves_the_connection() {
    let running = start(quick(), true).await;
    let (send, _driver) = h2_client(&running).await;
    let mut ready = send.clone().ready().await.unwrap();
    let (response, _) = ready
        .send_request(request("GET", "/abort").body(()).unwrap(), true)
        .unwrap();
    let (_, mut body) = timeout(WAIT, response).await.unwrap().unwrap().into_parts();
    let mut seen = Vec::new();
    let failure = loop {
        match timeout(WAIT, body.data()).await.unwrap() {
            Some(Ok(chunk)) => seen.extend_from_slice(&chunk),
            Some(Err(error)) => break Some(error),
            None => break None,
        }
    };
    // The client may drop data it had not read yet when the reset arrives.
    assert!(b"partial".starts_with(&seen), "{seen:?}");
    assert!(
        failure.is_some_and(|error| error.is_reset()),
        "no RST_STREAM"
    );
    let (parts, body) = call(&send, request("GET", "/").body(()).unwrap(), None).await;
    assert_eq!(
        (parts.status.as_u16(), body.as_slice()),
        (200, &b"hello"[..])
    );
    running.stop.send(true).unwrap();
}

#[tokio::test]
async fn a_client_reset_closes_the_producers_channel() {
    let running = start(quick(), true).await;
    let (send, _driver) = h2_client(&running).await;
    let mut ready = send.clone().ready().await.unwrap();
    let (response, mut stream) = ready
        .send_request(request("GET", "/gone").body(()).unwrap(), true)
        .unwrap();
    let (_, mut body) = timeout(WAIT, response).await.unwrap().unwrap().into_parts();
    let first = timeout(WAIT, body.data()).await.unwrap().unwrap().unwrap();
    assert_eq!(first, "first");
    stream.send_reset(h2::Reason::CANCEL);
    timeout(WAIT, running.backend.producer_gone.notified())
        .await
        .expect("the producer learned the client was gone");
    running.stop.send(true).unwrap();
}

#[tokio::test]
async fn a_file_is_sent_whole_in_frames() {
    let running = start(quick(), true).await;
    let (send, _driver) = h2_client(&running).await;
    let (parts, body) = call(&send, request("GET", "/file").body(()).unwrap(), None).await;
    assert_eq!(parts.status, 200);
    assert_eq!(parts.headers["content-length"], BIG_FILE_LEN.to_string());
    let expected: Vec<u8> = (0..BIG_FILE_LEN).map(|n| (n % 251) as u8).collect();
    assert_eq!(body, expected);
    running.stop.send(true).unwrap();
}

#[tokio::test]
async fn a_slow_but_progressing_download_is_not_cut() {
    // The reader pauses 150 ms against a 2 s write timeout, many times over.
    let config = ServerConfig {
        write_timeout: Duration::from_secs(2),
        ..stall_config()
    };
    let running = start(config, true).await;
    let (send, _driver) = h2_client(&running).await;
    let ready = send.clone().ready().await.unwrap();
    let (response, _) = {
        let mut ready = ready;
        ready
            .send_request(request("GET", "/big").body(()).unwrap(), true)
            .unwrap()
    };
    let (parts, mut body) = timeout(WAIT, response).await.unwrap().unwrap().into_parts();
    assert_eq!(parts.status, 200);
    let started = std::time::Instant::now();
    let mut size = 0;
    let mut since_pause = 0;
    while let Some(chunk) = body.data().await {
        let chunk = chunk.unwrap();
        size += chunk.len();
        since_pause += chunk.len();
        body.flow_control().release_capacity(chunk.len()).unwrap();
        // A reader that takes its time, but never stops for `write_timeout`.
        if since_pause >= 256 * 1024 {
            since_pause = 0;
            sleep(Duration::from_millis(150)).await;
        }
    }
    assert_eq!(size, 5 * MEBIBYTE);
    assert!(
        started.elapsed() > Duration::from_millis(2200),
        "too fast to prove anything: {:?}",
        started.elapsed()
    );
    running.stop.send(true).unwrap();
}

#[tokio::test]
async fn a_tail_stuck_in_the_window_is_watched_and_new_requests_do_not_save_the_connection() {
    let running = start(stall_config(), true).await;
    // One byte of window: "hello" cannot leave, whatever the client asks next.
    let mut raw = raw_h2_with(&running, &[0, 4, 0, 0, 0, 1]).await;
    raw.write_all(&get_frame(1, "/other")).await.unwrap();
    let (mut reader, mut writer) = tokio::io::split(raw);
    let flood = async {
        for n in 1..40u32 {
            if writer
                .write_all(&get_frame(2 * n + 1, "/other"))
                .await
                .is_err()
            {
                return;
            }
            sleep(Duration::from_millis(500)).await;
        }
    };
    let goaway = timeout(Duration::from_secs(3), async {
        tokio::select! {
            found = async {
                while let Some((kind, ..)) = next_frame(&mut reader).await {
                    if kind == 7 {
                        return true;
                    }
                }
                false
            } => found,
            () = flood => false,
        }
    })
    .await;
    assert_eq!(goaway, Ok(true), "a stuck tail kept the connection alive");
    running.stop.send(true).unwrap();
}

#[tokio::test]
async fn cookie_crumbs_do_not_hide_too_many_distinct_headers() {
    let running = start(quick(), true).await;
    let (send, _driver) = h2_client(&running).await;
    let mut many = request("GET", "/info");
    for n in 0..100 {
        many = many.header(format!("x-n{n}"), "v");
    }
    for n in 0..90 {
        many = many.header("cookie", format!("c{n}=v"));
    }
    let (parts, _) = call(&send, many.body(()).unwrap(), None).await;
    assert_eq!(parts.status, 431);
    running.stop.send(true).unwrap();
}

#[tokio::test]
async fn a_compressed_answer_over_h2_is_streamed_without_a_length() {
    let compression = kiso::Compression::new(kiso::CompressionConfig::default()).unwrap();
    let config = ServerConfig {
        compression: Some(Arc::new(compression)),
        ..quick()
    };
    let running = start(config, true).await;
    let (send, _driver) = h2_client(&running).await;
    let get = request("GET", "/big")
        .header("accept-encoding", "gzip")
        .body(())
        .unwrap();
    let (parts, body) = call(&send, get, None).await;
    assert_eq!(parts.headers["content-encoding"], "gzip");
    assert_eq!(parts.headers["vary"], "accept-encoding");
    assert!(!parts.headers.contains_key("content-length"));
    let mut plain = Vec::new();
    std::io::Read::read_to_end(&mut flate2::read::GzDecoder::new(&body[..]), &mut plain).unwrap();
    assert_eq!(plain, vec![b'x'; 5 * MEBIBYTE]);
    let plain = request("GET", "/big").body(()).unwrap();
    let (parts, body) = call(&send, plain, None).await;
    assert!(!parts.headers.contains_key("content-encoding"));
    assert_eq!(body.len(), 5 * MEBIBYTE);
    running.stop.send(true).unwrap();
}
