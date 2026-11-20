//! Streamed responses over a real loopback socket: the head arrives before the
//! body is complete, a gone client reaches the producer, and a failed producer
//! is never mistaken for a complete body.

use std::net::SocketAddr;
use std::sync::mpsc::Receiver;
use std::sync::Arc;
use std::time::Duration;

use bytes::Bytes;
use kiso::{
    serve, Aborted, AccessLog, Answer, Backend, Delivery, Incoming, LogFilter, ServerConfig, Sink,
    Streamed,
};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{mpsc, watch, Notify};
use tokio::time::timeout;

const WAIT: Duration = Duration::from_secs(5);

#[derive(Default)]
struct Producer {
    release: Arc<Notify>,
}

impl Backend for Producer {
    async fn handle(&self, _request: Incoming) -> Answer {
        unreachable!("every request is delivered")
    }

    async fn deliver(&self, request: Incoming) -> Delivery {
        let (sender, chunks) = mpsc::channel(1);
        let mut headers = vec![(b"content-type".to_vec(), b"text/plain".to_vec())];
        match request.path.as_str() {
            "/two" => self.two(sender),
            "/forever" => self.forever(sender),
            "/short" | "/long" => {
                let declared: &[u8] = if request.path == "/short" {
                    b"100"
                } else {
                    b"1"
                };
                headers.push((b"content-length".to_vec(), declared.to_vec()));
                Self::hi(sender);
            }
            "/crlf" => {
                headers.push((b"x-bad".to_vec(), b"a\r\nx-evil: 1".to_vec()));
                Self::hi(sender);
            }
            _ => self.broken(sender),
        }
        Delivery::Streamed(Streamed {
            status: 200,
            headers,
            chunks,
        })
    }
}

impl Producer {
    fn hi(sender: mpsc::Sender<kiso::Chunk>) {
        tokio::spawn(async move {
            let _ = sender.send(Ok(Bytes::from_static(b"hi"))).await;
        });
    }

    fn two(&self, sender: mpsc::Sender<kiso::Chunk>) {
        let release = Arc::clone(&self.release);
        tokio::spawn(async move {
            sender.send(Ok(Bytes::from_static(b"first"))).await.unwrap();
            release.notified().await;
            sender
                .send(Ok(Bytes::from_static(b"second")))
                .await
                .unwrap();
        });
    }

    fn forever(&self, sender: mpsc::Sender<kiso::Chunk>) {
        tokio::spawn(async move {
            while sender.send(Ok(Bytes::from(vec![b'x'; 1024]))).await.is_ok() {}
        });
    }

    fn broken(&self, sender: mpsc::Sender<kiso::Chunk>) {
        tokio::spawn(async move {
            sender
                .send(Ok(Bytes::from_static(b"partial")))
                .await
                .unwrap();
            sender.send(Err(Aborted)).await.unwrap();
        });
    }
}

async fn start(backend: Arc<Producer>) -> (SocketAddr, watch::Sender<bool>) {
    start_with(backend, ServerConfig::default()).await
}

async fn start_with(
    backend: Arc<Producer>,
    config: ServerConfig,
) -> (SocketAddr, watch::Sender<bool>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let (stop, receiver) = watch::channel(false);
    tokio::spawn(serve(listener, backend, config, None, receiver));
    (addr, stop)
}

/// A server whose access log records go to the returned receiver only.
async fn logged() -> (SocketAddr, watch::Sender<bool>, Receiver<String>) {
    let (sink, records) = Sink::new(16);
    let config = ServerConfig {
        access_log: Some(AccessLog::Logger),
        log_filter: Arc::new(LogFilter {
            sink: Some(sink),
            ..LogFilter::default()
        }),
        ..ServerConfig::default()
    };
    let (addr, stop) = start_with(Arc::new(Producer::default()), config).await;
    (addr, stop, records)
}

async fn everything(addr: SocketAddr, path: &str) -> String {
    let mut stream = request(addr, "GET", path).await;
    let mut read = Vec::new();
    let _ = timeout(WAIT, stream.read_to_end(&mut read))
        .await
        .expect("the connection closes");
    String::from_utf8_lossy(&read).to_string()
}

async fn request(addr: SocketAddr, method: &str, path: &str) -> TcpStream {
    let mut stream = TcpStream::connect(addr).await.unwrap();
    let text = format!("{method} {path} HTTP/1.1\r\nhost: x\r\n\r\n");
    stream.write_all(text.as_bytes()).await.unwrap();
    stream
}

async fn read_until(stream: &mut TcpStream, needle: &[u8]) -> Vec<u8> {
    let mut seen = Vec::new();
    let mut byte = [0u8; 1];
    while !seen.windows(needle.len()).any(|w| w == needle) {
        let read = timeout(WAIT, stream.read(&mut byte)).await.expect("data");
        match read.unwrap() {
            0 => break,
            _ => seen.push(byte[0]),
        }
    }
    seen
}

#[tokio::test]
async fn the_head_and_first_chunk_arrive_before_the_body_is_complete() {
    let backend = Arc::new(Producer::default());
    let (addr, _stop) = start(Arc::clone(&backend)).await;
    let mut stream = request(addr, "GET", "/two").await;
    let early = read_until(&mut stream, b"first").await;
    let text = String::from_utf8_lossy(&early).to_ascii_lowercase();
    assert!(text.starts_with("http/1.1 200"), "{text}");
    assert!(text.contains("transfer-encoding: chunked"), "{text}");
    backend.release.notify_one();
    let rest = read_until(&mut stream, b"0\r\n\r\n").await;
    let rest = String::from_utf8_lossy(&rest).to_string();
    assert!(rest.contains("second"), "{rest}");
}

#[tokio::test]
async fn a_failed_producer_never_ends_the_body_as_complete() {
    let (addr, _stop) = start(Arc::new(Producer::default())).await;
    let mut stream = request(addr, "GET", "/broken").await;
    let mut everything = Vec::new();
    let _ = timeout(WAIT, stream.read_to_end(&mut everything))
        .await
        .expect("the connection closes");
    let text = String::from_utf8_lossy(&everything).to_string();
    assert!(text.contains("partial"), "{text}");
    assert!(!text.ends_with("0\r\n\r\n"), "{text}");
}

#[tokio::test]
async fn a_client_that_leaves_stops_the_producer() {
    let (addr, _stop) = start(Arc::new(Producer::default())).await;
    let mut stream = request(addr, "GET", "/forever").await;
    read_until(&mut stream, b"xxxx").await;
    drop(stream);
    // The producer loop ends when its send fails; a leaked one would keep the
    // runtime busy, so the server must still answer a new request promptly.
    let mut next = request(addr, "GET", "/two").await;
    let head = read_until(&mut next, b"first").await;
    assert!(String::from_utf8_lossy(&head).contains("first"));
}

#[tokio::test]
async fn head_gets_the_head_and_no_body() {
    let (addr, _stop) = start(Arc::new(Producer::default())).await;
    let mut stream = request(addr, "HEAD", "/two").await;
    let head = read_until(&mut stream, b"\r\n\r\n").await;
    let text = String::from_utf8_lossy(&head).to_string();
    assert!(text.starts_with("HTTP/1.1 200"), "{text}");
    assert!(!text.contains("first"));
}

#[tokio::test]
async fn a_body_shorter_than_its_content_length_closes_the_connection() {
    let (addr, _stop, _records) = logged().await;
    let text = everything(addr, "/short").await;
    assert!(text.starts_with("HTTP/1.1 200"), "{text}");
    let (_, body) = text.split_once("\r\n\r\n").expect("a head");
    assert!("hi".starts_with(body), "{text}");
}

#[tokio::test]
async fn a_body_longer_than_its_content_length_closes_the_connection() {
    let (addr, _stop, _records) = logged().await;
    let text = everything(addr, "/long").await;
    assert!(text.starts_with("HTTP/1.1 200"), "{text}");
    assert!(!text.ends_with("hi"), "{text}");
}

#[tokio::test]
async fn a_head_the_transport_refuses_is_logged_as_the_500_it_became() {
    let (addr, _stop, records) = logged().await;
    let mut stream = request(addr, "GET", "/crlf").await;
    let head = read_until(&mut stream, b"\r\n\r\n").await;
    let text = String::from_utf8_lossy(&head).to_string();
    assert!(text.starts_with("HTTP/1.1 500"), "{text}");
    assert!(!text.contains("x-evil"), "{text}");
    let record = records.recv_timeout(WAIT).expect("an access record");
    assert!(record.contains("\"status\":500"), "{record}");
}
