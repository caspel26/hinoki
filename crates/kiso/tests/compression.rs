//! Response compression over real HTTP/1.1 loopback sockets: a stream is
//! flushed when its producer pauses, a body that is too large for one step is
//! chunked, HEAD announces the coding, and a full set of slots answers as is.

use std::io::Read;
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use bytes::Bytes;
use kiso::{
    serve, Answer, Backend, Compression, CompressionConfig, Delivery, Incoming, Pace, ServerConfig,
    Streamed,
};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{mpsc, watch, Notify};
use tokio::time::timeout;

const WAIT: Duration = Duration::from_secs(5);
const TEXT: &[u8] = b"a line of text that repeats\n";

#[derive(Default)]
struct Producer {
    release: Arc<Notify>,
}

fn text_headers(length: Option<usize>) -> Vec<(Vec<u8>, Vec<u8>)> {
    let mut headers = vec![(b"content-type".to_vec(), b"text/plain".to_vec())];
    if let Some(length) = length {
        headers.push((b"content-length".to_vec(), length.to_string().into_bytes()));
    }
    headers
}

impl Backend for Producer {
    async fn handle(&self, _request: Incoming) -> Answer {
        unreachable!("every request is delivered")
    }

    async fn deliver(&self, request: Incoming) -> Delivery {
        if request.path == "/whole" {
            let body = TEXT.repeat(40_000);
            return Delivery::Whole(Answer {
                status: 200,
                headers: text_headers(Some(body.len())),
                body: Bytes::from(body),
            });
        }
        let (sender, chunks) = mpsc::channel(1);
        let release = Arc::clone(&self.release);
        tokio::spawn(async move {
            sender
                .send(Ok(Bytes::from_static(b"first;")))
                .await
                .unwrap();
            release.notified().await;
            let _ = sender.send(Ok(Bytes::from_static(b"last;"))).await;
        });
        Delivery::Streamed(Streamed {
            status: 200,
            headers: text_headers(None),
            chunks,
            pace: Pace::default(),
        })
    }
}

async fn start(backend: Arc<Producer>, slots: usize) -> (SocketAddr, watch::Sender<bool>) {
    let config = CompressionConfig {
        min_size: 0,
        max_concurrent: slots,
        ..CompressionConfig::default()
    };
    let config = ServerConfig {
        compression: Some(Arc::new(Compression::new(config).unwrap())),
        ..ServerConfig::default()
    };
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let (stop, receiver) = watch::channel(false);
    tokio::spawn(serve(listener, backend, config, None, receiver));
    (addr, stop)
}

async fn request(addr: SocketAddr, method: &str, path: &str, coding: &str) -> TcpStream {
    let mut stream = TcpStream::connect(addr).await.unwrap();
    let text = format!(
        "{method} {path} HTTP/1.1\r\nhost: x\r\naccept-encoding: {coding}\r\nconnection: close\r\n\r\n"
    );
    stream.write_all(text.as_bytes()).await.unwrap();
    stream
}

async fn head_of(stream: &mut TcpStream) -> String {
    let mut seen = Vec::new();
    let mut byte = [0u8; 1];
    while !seen.ends_with(b"\r\n\r\n") {
        let read = timeout(WAIT, stream.read(&mut byte)).await.expect("head");
        assert_eq!(read.unwrap(), 1, "the head ended early");
        seen.push(byte[0]);
    }
    String::from_utf8(seen).unwrap().to_ascii_lowercase()
}

async fn next_chunk(stream: &mut TcpStream) -> Vec<u8> {
    let mut size = Vec::new();
    let mut byte = [0u8; 1];
    while !size.ends_with(b"\r\n") {
        timeout(WAIT, stream.read_exact(&mut byte))
            .await
            .expect("chunk size")
            .unwrap();
        size.push(byte[0]);
    }
    let length =
        usize::from_str_radix(std::str::from_utf8(&size[..size.len() - 2]).unwrap(), 16).unwrap();
    let mut data = vec![0u8; length + 2];
    timeout(WAIT, stream.read_exact(&mut data))
        .await
        .expect("chunk")
        .unwrap();
    data.truncate(length);
    data
}

fn dechunk(mut data: &[u8]) -> Vec<u8> {
    let mut body = Vec::new();
    while let Some(end) = data.windows(2).position(|pair| pair == b"\r\n") {
        let length = usize::from_str_radix(std::str::from_utf8(&data[..end]).unwrap(), 16).unwrap();
        if length == 0 {
            break;
        }
        body.extend_from_slice(&data[end + 2..end + 2 + length]);
        data = &data[end + 2 + length + 2..];
    }
    body
}

async fn everything(mut stream: TcpStream) -> (String, Vec<u8>) {
    let mut read = Vec::new();
    timeout(WAIT, stream.read_to_end(&mut read))
        .await
        .expect("the connection closes")
        .unwrap();
    let split = read.windows(4).position(|w| w == b"\r\n\r\n").unwrap() + 4;
    let head = String::from_utf8_lossy(&read[..split]).to_ascii_lowercase();
    (head, read[split..].to_vec())
}

fn gunzip(bytes: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    flate2::read::GzDecoder::new(bytes)
        .read_to_end(&mut out)
        .unwrap();
    out
}

#[tokio::test]
async fn a_stream_is_flushed_when_its_producer_pauses() {
    let backend = Arc::new(Producer::default());
    let (addr, _stop) = start(Arc::clone(&backend), 4).await;
    let mut stream = request(addr, "GET", "/stream", "gzip").await;
    let head = head_of(&mut stream).await;
    assert!(head.contains("content-encoding: gzip"), "{head}");
    assert!(head.contains("transfer-encoding: chunked"), "{head}");
    let mut decoder = flate2::write::GzDecoder::new(Vec::new());
    while decoder.get_ref().is_empty() {
        std::io::Write::write_all(&mut decoder, &next_chunk(&mut stream).await).unwrap();
        std::io::Write::flush(&mut decoder).unwrap();
    }
    assert_eq!(decoder.get_ref().as_slice(), b"first;");
    backend.release.notify_one();
    let mut rest = Vec::new();
    timeout(WAIT, stream.read_to_end(&mut rest))
        .await
        .expect("the connection closes")
        .unwrap();
    std::io::Write::write_all(&mut decoder, &dechunk(&rest)).unwrap();
    assert_eq!(decoder.finish().unwrap(), b"first;last;");
}

#[tokio::test]
async fn a_large_whole_body_is_compressed_in_chunks() {
    let (addr, _stop) = start(Arc::new(Producer::default()), 4).await;
    let (head, body) = everything(request(addr, "GET", "/whole", "gzip").await).await;
    assert!(head.contains("content-encoding: gzip"), "{head}");
    assert!(!head.contains("content-length"), "{head}");
    assert_eq!(gunzip(&dechunk(&body)), TEXT.repeat(40_000));
}

#[tokio::test]
async fn head_announces_the_coding_without_a_length() {
    let (addr, _stop) = start(Arc::new(Producer::default()), 4).await;
    let (head, body) = everything(request(addr, "HEAD", "/whole", "br").await).await;
    assert!(head.contains("content-encoding: br"), "{head}");
    assert!(head.contains("vary: accept-encoding"), "{head}");
    assert!(!head.contains("content-length"), "{head}");
    assert!(body.is_empty());
}

#[tokio::test]
async fn a_full_set_of_slots_answers_without_compression() {
    let backend = Arc::new(Producer::default());
    let (addr, _stop) = start(Arc::clone(&backend), 1).await;
    let mut held = request(addr, "GET", "/stream", "gzip").await;
    assert!(head_of(&mut held).await.contains("content-encoding: gzip"));
    let (head, body) = everything(request(addr, "GET", "/whole", "gzip").await).await;
    assert!(!head.contains("content-encoding"), "{head}");
    assert_eq!(body, TEXT.repeat(40_000));
    backend.release.notify_one();
}
