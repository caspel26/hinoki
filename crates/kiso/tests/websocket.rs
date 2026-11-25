//! WebSocket upgrades over real loopback sockets, with a fake backend that
//! echoes every message.

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use kiso::wsrun::{Accepted, Config, Outbound, Session, Shared};
use kiso::{serve, Answer, Backend, Closed, Delivery, Incoming, ServerConfig};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{mpsc, watch};
use tokio::task::JoinHandle;
use tokio::time::timeout;

const WAIT: Duration = Duration::from_secs(5);
const KEY: &str = "dGhlIHNhbXBsZSBub25jZQ==";
const ACCEPT: &str = "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=";

struct Echo {
    shared: Arc<Shared>,
}

impl Backend for Echo {
    async fn handle(&self, _request: Incoming) -> Answer {
        Answer {
            status: 200,
            headers: vec![(b"content-length".to_vec(), b"2".to_vec())],
            body: bytes::Bytes::from_static(b"ok"),
        }
    }

    async fn deliver(&self, request: Incoming) -> Delivery {
        if request.path != "/ws" && request.path != "/ws-bad-protocol" {
            return Delivery::Whole(self.handle(request).await);
        }
        let (inbound, mut from_client) = mpsc::channel(4);
        let (to_client, outbound) = mpsc::channel(4);
        let subprotocol = Some(
            if request.path == "/ws" {
                "chat"
            } else {
                "other"
            }
            .to_owned(),
        );
        tokio::spawn(async move {
            while let Some(message) = from_client.recv().await {
                if to_client.send(Outbound::Message(message)).await.is_err() {
                    break;
                }
            }
        });
        Delivery::Upgrade(Box::new(Accepted {
            subprotocol,
            headers: Vec::new(),
            session: Session {
                config: Config {
                    ping_interval: None,
                    close_timeout: Duration::from_millis(300),
                    ..Config::default()
                },
                inbound,
                outbound,
                shared: Arc::clone(&self.shared),
                hold: None,
            },
        }))
    }
}

struct Running {
    addr: SocketAddr,
    shared: Arc<Shared>,
    stop: watch::Sender<bool>,
    task: JoinHandle<Closed>,
}

async fn start() -> Running {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let shared = Arc::new(Shared::default());
    let backend = Arc::new(Echo {
        shared: Arc::clone(&shared),
    });
    let (stop, receiver) = watch::channel(false);
    let config = ServerConfig {
        shutdown_timeout: Duration::from_secs(2),
        ..ServerConfig::default()
    };
    let task = tokio::spawn(serve(listener, backend, config, None, receiver));
    Running {
        addr,
        shared,
        stop,
        task,
    }
}

fn handshake(path: &str, extra: &str) -> String {
    format!(
        "GET {path} HTTP/1.1\r\nhost: t\r\nconnection: Upgrade\r\nupgrade: websocket\r\n\
         sec-websocket-version: 13\r\nsec-websocket-key: {KEY}\r\n\
         sec-websocket-protocol: chat\r\n{extra}\r\n"
    )
}

/// The response head, without any bytes after it.
async fn head_of(stream: &mut TcpStream) -> String {
    let mut raw = Vec::new();
    while !raw.ends_with(b"\r\n\r\n") {
        let mut byte = [0_u8; 1];
        timeout(WAIT, stream.read_exact(&mut byte))
            .await
            .unwrap()
            .unwrap();
        raw.push(byte[0]);
    }
    String::from_utf8(raw).unwrap()
}

async fn upgraded(running: &Running) -> TcpStream {
    let mut stream = TcpStream::connect(running.addr).await.unwrap();
    stream
        .write_all(handshake("/ws", "").as_bytes())
        .await
        .unwrap();
    let head = head_of(&mut stream).await;
    assert!(head.starts_with("HTTP/1.1 101"), "{head}");
    stream
}

async fn frame(stream: &mut TcpStream) -> (u8, Vec<u8>) {
    let mut head = [0_u8; 2];
    timeout(WAIT, stream.read_exact(&mut head))
        .await
        .unwrap()
        .unwrap();
    let mut payload = vec![0; usize::from(head[1] & 0x7F)];
    stream.read_exact(&mut payload).await.unwrap();
    (head[0] & 0x0F, payload)
}

async fn say(stream: &mut TcpStream, opcode: u8, payload: &[u8]) {
    let bytes = kiso::ws::masked_frame(opcode, true, payload, [3, 1, 4, 1]);
    stream.write_all(&bytes).await.unwrap();
}

#[tokio::test]
async fn the_handshake_is_accepted_with_the_chosen_subprotocol() {
    let running = start().await;
    let mut stream = TcpStream::connect(running.addr).await.unwrap();
    stream
        .write_all(handshake("/ws", "").as_bytes())
        .await
        .unwrap();
    let head = head_of(&mut stream).await.to_ascii_lowercase();
    assert!(head.starts_with("http/1.1 101"), "{head}");
    assert!(head.contains("upgrade: websocket"), "{head}");
    assert!(
        head.contains(&format!(
            "sec-websocket-accept: {}",
            ACCEPT.to_ascii_lowercase()
        )),
        "{head}"
    );
    assert!(head.contains("sec-websocket-protocol: chat"), "{head}");
}

#[tokio::test]
async fn messages_are_echoed_and_their_bytes_are_never_rewritten() {
    let running = start().await;
    let mut stream = upgraded(&running).await;
    // A bare LF would be rewritten to NUL if the HTTP guard still scanned.
    say(&mut stream, kiso::ws::TEXT, b"a\nb\r\n\r\nc").await;
    assert_eq!(
        frame(&mut stream).await,
        (kiso::ws::TEXT, b"a\nb\r\n\r\nc".to_vec())
    );
    say(&mut stream, kiso::ws::BINARY, &[0, 255, 10]).await;
    assert_eq!(
        frame(&mut stream).await,
        (kiso::ws::BINARY, vec![0, 255, 10])
    );
}

#[tokio::test]
async fn a_close_ends_the_session_and_frees_the_connection() {
    let running = start().await;
    let mut stream = upgraded(&running).await;
    say(&mut stream, kiso::ws::CLOSE, &1000_u16.to_be_bytes()).await;
    assert_eq!(frame(&mut stream).await.0, kiso::ws::CLOSE);
    let mut rest = Vec::new();
    timeout(WAIT, stream.read_to_end(&mut rest))
        .await
        .unwrap()
        .unwrap();
    assert_eq!(running.shared.closed().unwrap().code, 1000);
}

#[tokio::test]
async fn a_shutdown_closes_open_sessions_with_1001() {
    let running = start().await;
    let mut stream = upgraded(&running).await;
    running.stop.send(true).unwrap();
    let (opcode, payload) = frame(&mut stream).await;
    assert_eq!(
        (opcode, &payload[..2]),
        (kiso::ws::CLOSE, &1001_u16.to_be_bytes()[..])
    );
    say(&mut stream, kiso::ws::CLOSE, &1001_u16.to_be_bytes()).await;
    let closed = timeout(WAIT, running.task).await.unwrap().unwrap();
    assert_eq!(closed.aborted, 0);
}

#[tokio::test]
async fn a_subprotocol_the_client_did_not_offer_is_a_server_error() {
    let running = start().await;
    let mut stream = TcpStream::connect(running.addr).await.unwrap();
    stream
        .write_all(handshake("/ws-bad-protocol", "").as_bytes())
        .await
        .unwrap();
    let head = head_of(&mut stream).await;
    assert!(head.starts_with("HTTP/1.1 500"), "{head}");
}

#[tokio::test]
async fn a_plain_request_on_the_same_server_is_still_http() {
    let running = start().await;
    let mut stream = TcpStream::connect(running.addr).await.unwrap();
    stream
        .write_all(b"GET /plain HTTP/1.1\r\nhost: t\r\n\r\n")
        .await
        .unwrap();
    assert!(head_of(&mut stream).await.starts_with("HTTP/1.1 200"));
}

#[tokio::test]
async fn a_message_over_the_limit_is_refused_with_1009() {
    let running = start().await;
    let mut stream = upgraded(&running).await;
    // The server's limit is 1 MiB: announce more without sending it.
    let mut head = vec![0x82, 0x80 | 127];
    head.extend_from_slice(&(2_u64 * 1024 * 1024).to_be_bytes());
    head.extend_from_slice(&[0; 4]);
    stream.write_all(&head).await.unwrap();
    let (opcode, payload) = frame(&mut stream).await;
    assert_eq!(
        (opcode, &payload[..2]),
        (kiso::ws::CLOSE, &1009_u16.to_be_bytes()[..])
    );
}
