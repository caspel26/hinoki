//! TLS over real loopback sockets, with the self-signed test certificate.

use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use hinoki_server::tls::acceptor;
use hinoki_server::{serve, Answer, Backend, Incoming, ServerConfig};
use rustls::crypto::ring::default_provider;
use rustls::pki_types::pem::PemObject;
use rustls::pki_types::{CertificateDer, ServerName};
use rustls::{ClientConfig, RootCertStore};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::watch;
use tokio::time::timeout;
use tokio_rustls::client::TlsStream;
use tokio_rustls::TlsConnector;

const WAIT: Duration = Duration::from_secs(5);

struct Echo;

impl Backend for Echo {
    async fn handle(&self, request: Incoming) -> Answer {
        let body = format!("secure={}", request.secure).into_bytes();
        Answer {
            status: 200,
            headers: vec![(
                b"content-length".to_vec(),
                body.len().to_string().into_bytes(),
            )],
            body,
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
    stop: watch::Sender<bool>,
}

async fn start(config: ServerConfig) -> Running {
    let tls = acceptor(
        &fixture("localhost-cert.pem"),
        &fixture("localhost-key.pem"),
    )
    .unwrap();
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let (stop, receiver) = watch::channel(false);
    tokio::spawn(serve(listener, Arc::new(Echo), config, Some(tls), receiver));
    Running { addr, stop }
}

fn client(versions: &[&'static rustls::SupportedProtocolVersion]) -> TlsConnector {
    let mut roots = RootCertStore::empty();
    for cert in CertificateDer::pem_file_iter(fixture("localhost-cert.pem")).unwrap() {
        roots.add(cert.unwrap()).unwrap();
    }
    let mut config = ClientConfig::builder_with_provider(Arc::new(default_provider()))
        .with_protocol_versions(versions)
        .unwrap()
        .with_root_certificates(roots)
        .with_no_client_auth();
    config.alpn_protocols = vec![b"http/1.1".to_vec()];
    TlsConnector::from(Arc::new(config))
}

async fn secure(
    running: &Running,
    connector: &TlsConnector,
) -> std::io::Result<TlsStream<TcpStream>> {
    let tcp = TcpStream::connect(running.addr).await.unwrap();
    connector
        .connect(ServerName::try_from("localhost").unwrap(), tcp)
        .await
}

async fn get(stream: &mut TlsStream<TcpStream>) -> String {
    stream
        .write_all(b"GET /x HTTP/1.1\r\nhost: localhost\r\n\r\n")
        .await
        .unwrap();
    let mut raw = Vec::new();
    let mut byte = [0u8; 1];
    while !raw.ends_with(b"secure=true") && !raw.ends_with(b"secure=false") {
        timeout(WAIT, stream.read_exact(&mut byte))
            .await
            .unwrap()
            .unwrap();
        raw.push(byte[0]);
    }
    String::from_utf8(raw).unwrap()
}

#[tokio::test]
async fn a_tls_client_is_served_and_the_request_is_secure() {
    let running = start(ServerConfig::default()).await;
    let mut stream = secure(&running, &client(rustls::ALL_VERSIONS))
        .await
        .unwrap();
    for _ in 0..2 {
        let answer = get(&mut stream).await;
        assert!(answer.starts_with("HTTP/1.1 200"), "{answer}");
        assert!(answer.ends_with("secure=true"), "{answer}");
    }
    assert_eq!(stream.get_ref().1.alpn_protocol(), Some(&b"http/1.1"[..]));
    running.stop.send(true).unwrap();
}

#[tokio::test]
async fn both_tls_versions_are_accepted() {
    let running = start(ServerConfig::default()).await;
    for version in [&rustls::version::TLS12, &rustls::version::TLS13] {
        let stream = secure(&running, &client(&[version])).await.unwrap();
        assert_eq!(stream.get_ref().1.protocol_version(), Some(version.version));
    }
    running.stop.send(true).unwrap();
}

#[tokio::test]
async fn plain_http_on_a_tls_port_is_closed_without_an_answer() {
    let running = start(ServerConfig::default()).await;
    let mut plain = TcpStream::connect(running.addr).await.unwrap();
    plain
        .write_all(b"GET /x HTTP/1.1\r\nhost: localhost\r\n\r\n")
        .await
        .unwrap();
    let mut buf = Vec::new();
    let _ = timeout(WAIT, plain.read_to_end(&mut buf)).await.unwrap();
    assert!(!buf.starts_with(b"HTTP/"), "answered a plaintext request");
    running.stop.send(true).unwrap();
}

#[tokio::test]
async fn a_client_that_never_handshakes_is_closed_by_the_header_read_timeout() {
    let config = ServerConfig {
        header_read_timeout: Duration::from_millis(300),
        ..ServerConfig::default()
    };
    let running = start(config).await;
    let mut silent = TcpStream::connect(running.addr).await.unwrap();
    let mut byte = [0u8; 1];
    let outcome = timeout(WAIT, silent.read(&mut byte)).await.unwrap();
    assert!(matches!(outcome, Ok(0) | Err(_)));
    running.stop.send(true).unwrap();
}
