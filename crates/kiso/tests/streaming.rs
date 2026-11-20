//! Streamed responses over a real loopback socket: the head arrives before the
//! body is complete, a gone client reaches the producer, and a failed producer
//! is never mistaken for a complete body.

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use bytes::Bytes;
use kiso::{serve, Aborted, Answer, Backend, Delivery, Incoming, ServerConfig, Streamed};
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
        match request.path.as_str() {
            "/two" => self.two(sender),
            "/forever" => self.forever(sender),
            _ => self.broken(sender),
        }
        Delivery::Streamed(Streamed {
            status: 200,
            headers: vec![(b"content-type".to_vec(), b"text/plain".to_vec())],
            chunks,
        })
    }
}

impl Producer {
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
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let (stop, receiver) = watch::channel(false);
    tokio::spawn(serve(
        listener,
        backend,
        ServerConfig::default(),
        None,
        receiver,
    ));
    (addr, stop)
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
