//! Request bodies over real loopback sockets: reading, limits, timeouts,
//! refusals, and what happens to the connection afterwards.

use std::net::SocketAddr;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use kiso::{serve, Answer, Backend, BodyError, BodyReader, Incoming, ServerConfig};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::watch;
use tokio::time::timeout;

const WAIT: Duration = Duration::from_secs(5);
const LIMIT: usize = 64;

#[derive(Default)]
struct Probe {
    /// Paths of the requests that reached the backend.
    seen: Mutex<Vec<String>>,
    /// How each body read ended.
    outcomes: Mutex<Vec<Result<Vec<u8>, BodyError>>>,
    /// Bodies a backend keeps alive after answering (a Python object that
    /// outlives the answer).
    held: Mutex<Vec<BodyReader>>,
}

fn answer(status: u16, body: &[u8]) -> Answer {
    Answer {
        status,
        headers: vec![(
            b"content-length".to_vec(),
            body.len().to_string().into_bytes(),
        )],
        body: bytes::Bytes::copy_from_slice(body),
    }
}

impl Backend for Probe {
    async fn handle(&self, mut request: Incoming) -> Answer {
        self.seen.lock().unwrap().push(request.path.clone());
        if let Some(early) = &request.refusal {
            return answer(early.status(), b"");
        }
        match request.path.as_str() {
            "/read" => {
                let Some(body) = request.body.take() else {
                    return answer(200, b"");
                };
                let outcome = body.read_all(LIMIT).await;
                let reply = match &outcome {
                    Ok(bytes) => answer(200, bytes),
                    Err(BodyError::TooLarge) => answer(413, b"too large"),
                    Err(BodyError::Timeout) => answer(408, b"timeout"),
                    Err(BodyError::Disconnected) => answer(400, b"disconnected"),
                    Err(BodyError::Malformed) => answer(400, b"malformed"),
                };
                self.outcomes.lock().unwrap().push(outcome);
                reply
            }
            "/hold" => {
                self.held.lock().unwrap().extend(request.body.take());
                answer(200, b"held")
            }
            _ => answer(200, b"unread"),
        }
    }
}

struct Running {
    addr: SocketAddr,
    probe: Arc<Probe>,
    stop: watch::Sender<bool>,
}

async fn start() -> Running {
    let config = ServerConfig {
        body_read_timeout: Duration::from_millis(300),
        ..ServerConfig::default()
    };
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let probe = Arc::new(Probe::default());
    let (stop, receiver) = watch::channel(false);
    tokio::spawn(serve(listener, Arc::clone(&probe), config, None, receiver));
    Running { addr, probe, stop }
}

impl Drop for Running {
    fn drop(&mut self) {
        let _ = self.stop.send(true);
    }
}

async fn connect(running: &Running) -> TcpStream {
    TcpStream::connect(running.addr).await.unwrap()
}

/// One response: head text and `content-length` body bytes (read only when the
/// head has one). `None` when the connection ended before a head.
async fn read_response(stream: &mut TcpStream) -> Option<(String, Vec<u8>)> {
    let mut raw = Vec::new();
    let mut byte = [0u8; 1];
    while !raw.ends_with(b"\r\n\r\n") {
        match timeout(WAIT, stream.read(&mut byte)).await.expect("head") {
            Ok(1) => raw.push(byte[0]),
            _ => return None,
        }
    }
    let head = String::from_utf8(raw).unwrap();
    let length = head
        .to_ascii_lowercase()
        .lines()
        .find_map(|l| l.strip_prefix("content-length: ").map(str::to_owned))
        .map_or(0, |v| v.parse::<usize>().unwrap());
    let mut body = vec![0u8; length];
    timeout(WAIT, stream.read_exact(&mut body))
        .await
        .expect("body")
        .unwrap();
    Some((head, body))
}

fn status(head: &str) -> u16 {
    head[9..12].parse().unwrap()
}

fn closes(head: &str) -> bool {
    head.to_ascii_lowercase().contains("connection: close")
}

async fn ended(stream: &mut TcpStream) -> bool {
    let mut byte = [0u8; 1];
    matches!(
        timeout(WAIT, stream.read(&mut byte)).await,
        Ok(Ok(0) | Err(_))
    )
}

/// Send `request`, read one response, report whether the connection ended.
async fn exchange(running: &Running, request: &[u8]) -> (u16, Vec<u8>, bool) {
    let mut stream = connect(running).await;
    stream.write_all(request).await.unwrap();
    let (head, body) = read_response(&mut stream).await.expect("a response");
    let ended = closes(&head) && ended(&mut stream).await;
    (status(&head), body, ended)
}

const POST: &str = "POST /read HTTP/1.1\r\nhost: t\r\n";

#[tokio::test]
async fn a_content_length_body_arrives_byte_identical_and_the_connection_is_reused() {
    let running = start().await;
    let mut stream = connect(&running).await;
    let json = b"{\n  \"a\": 1,\n  \"b\": [\n    2\n  ]\n}";
    let request = format!("{POST}content-length: {}\r\n\r\n", json.len());
    stream.write_all(request.as_bytes()).await.unwrap();
    stream.write_all(json).await.unwrap();
    let (head, body) = read_response(&mut stream).await.unwrap();
    assert_eq!((status(&head), body.as_slice()), (200, &json[..]));
    assert!(!closes(&head), "{head}");
    stream
        .write_all(b"GET /other HTTP/1.1\r\nhost: t\r\n\r\n")
        .await
        .unwrap();
    let (head, _) = read_response(&mut stream).await.unwrap();
    assert_eq!(status(&head), 200);
}

#[tokio::test]
async fn a_well_formed_request_pipelined_after_a_body_is_answered() {
    let running = start().await;
    let mut stream = connect(&running).await;
    let request =
        format!("{POST}content-length: 2\r\n\r\nabGET /second HTTP/1.1\r\nhost: t\r\n\r\n");
    stream.write_all(request.as_bytes()).await.unwrap();
    let (head, body) = read_response(&mut stream).await.unwrap();
    assert_eq!((status(&head), body.as_slice()), (200, &b"ab"[..]));
    let (head, _) = read_response(&mut stream).await.unwrap();
    assert_eq!(status(&head), 200);
    assert_eq!(*running.probe.seen.lock().unwrap(), ["/read", "/second"]);
}

#[tokio::test]
async fn a_chunked_body_arrives_byte_identical() {
    let running = start().await;
    let request = format!(
        "{POST}transfer-encoding: chunked\r\n\r\n4\r\n{{\n  \r\n6\r\n\"a\":1\n\r\n1\r\n}}\r\n0\r\n\r\n"
    );
    let (code, body, _) = exchange(&running, request.as_bytes()).await;
    assert_eq!((code, body.as_slice()), (200, &b"{\n  \"a\":1\n}"[..]));
}

#[tokio::test]
async fn a_chunked_request_closes_the_connection_after_the_answer() {
    let running = start().await;
    let request = format!("{POST}transfer-encoding: chunked\r\n\r\n2\r\nab\r\n0\r\n\r\n");
    let (code, _, ended) = exchange(&running, request.as_bytes()).await;
    assert_eq!(code, 200);
    assert!(ended);
}

/// Write `body` after the head while another task reads the answer, as a real
/// client does; the server may answer long before it has read everything.
async fn send_and_receive(
    running: &Running,
    head: String,
    body: Vec<u8>,
) -> (String, Vec<u8>, bool) {
    let stream = connect(running).await;
    let (mut read, mut write) = stream.into_split();
    let writer = tokio::spawn(async move {
        let _ = write.write_all(head.as_bytes()).await;
        let _ = write.write_all(&body).await;
        write
    });
    let mut reunited = Vec::new();
    let mut byte = [0u8; 1];
    while !reunited.ends_with(b"\r\n\r\n") {
        match timeout(WAIT, read.read(&mut byte)).await.expect("head") {
            Ok(1) => reunited.push(byte[0]),
            other => panic!("the answer was lost: {other:?}"),
        }
    }
    let head = String::from_utf8(reunited).unwrap();
    let length = head
        .to_ascii_lowercase()
        .lines()
        .find_map(|l| l.strip_prefix("content-length: ").map(str::to_owned))
        .map_or(0, |v| v.parse::<usize>().unwrap());
    let mut answer = vec![0u8; length];
    timeout(WAIT, read.read_exact(&mut answer))
        .await
        .expect("body")
        .expect("the body of the answer");
    let mut rest = [0u8; 1];
    let ended = matches!(
        timeout(WAIT, read.read(&mut rest)).await,
        Ok(Ok(0) | Err(_))
    );
    writer.abort();
    (head, answer, ended)
}

#[tokio::test]
async fn a_declared_length_over_the_limit_is_refused_and_the_answer_is_not_lost() {
    let running = start().await;
    let size = 512 * 1024;
    let head = format!("{POST}content-length: {size}\r\n\r\n");
    let (head, body, ended) = send_and_receive(&running, head, vec![b'x'; size]).await;
    assert_eq!((status(&head), body.as_slice()), (413, &b"too large"[..]));
    assert!(closes(&head) && ended);
}

#[tokio::test]
async fn a_chunked_total_over_the_limit_is_refused_and_the_answer_is_not_lost() {
    let running = start().await;
    let head = format!("{POST}transfer-encoding: chunked\r\n\r\n");
    let mut body = Vec::new();
    for _ in 0..4096 {
        body.extend_from_slice(b"80\r\n");
        body.extend_from_slice(&[b'y'; 128]);
        body.extend_from_slice(b"\r\n");
    }
    body.extend_from_slice(b"0\r\n\r\n");
    let (head, answer, ended) = send_and_receive(&running, head, body).await;
    assert_eq!((status(&head), answer.as_slice()), (413, &b"too large"[..]));
    assert!(closes(&head) && ended);
}

#[tokio::test]
async fn the_limit_itself_is_accepted() {
    let running = start().await;
    let body = vec![b'z'; LIMIT];
    let request = format!("{POST}content-length: {LIMIT}\r\n\r\n");
    let mut bytes = request.into_bytes();
    bytes.extend_from_slice(&body);
    let (code, answer, _) = exchange(&running, &bytes).await;
    assert_eq!((code, answer), (200, body));
}

#[tokio::test]
async fn a_stalled_body_gets_408_and_the_connection_closes() {
    let running = start().await;
    let mut stream = connect(&running).await;
    stream
        .write_all(format!("{POST}content-length: 10\r\n\r\nabc").as_bytes())
        .await
        .unwrap();
    let (head, body) = read_response(&mut stream).await.unwrap();
    assert_eq!((status(&head), body.as_slice()), (408, &b"timeout"[..]));
    assert!(closes(&head));
    assert!(ended(&mut stream).await);
}

#[tokio::test]
async fn a_client_that_leaves_mid_body_is_a_disconnect() {
    let running = start().await;
    let mut stream = connect(&running).await;
    stream
        .write_all(format!("{POST}content-length: 10\r\n\r\nabc").as_bytes())
        .await
        .unwrap();
    drop(stream);
    timeout(WAIT, async {
        while running.probe.outcomes.lock().unwrap().is_empty() {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    assert_eq!(
        running.probe.outcomes.lock().unwrap()[0],
        Err(BodyError::Disconnected)
    );
}

#[tokio::test]
async fn a_bare_lf_in_a_head_is_a_400_and_nothing_reaches_the_backend() {
    let running = start().await;
    for request in [
        &b"GET /a HTTP/1.1\r\nhost: t\r\nx: a\ncontent-length: 5\r\n\r\n"[..],
        b"GET /a HTTP/1.1\nhost: t\r\n\r\n",
        b"GET /a HTTP/1.1\r\nhost: t\n\r\n",
        b"GET /a HTTP/1.1\r\nhost: t\r\n\n",
    ] {
        let (code, _, ended) = exchange(&running, request).await;
        assert_eq!(code, 400, "{}", String::from_utf8_lossy(request));
        assert!(ended);
    }
    assert!(running.probe.seen.lock().unwrap().is_empty());
}

#[tokio::test]
async fn a_bare_lf_in_a_pipelined_head_after_a_body_is_caught() {
    let running = start().await;
    let mut stream = connect(&running).await;
    let first = format!("{POST}content-length: 2\r\n\r\nab");
    let second = "GET /second HTTP/1.1\r\nhost: t\nx: y\r\n\r\n";
    stream
        .write_all(format!("{first}{second}").as_bytes())
        .await
        .unwrap();
    // The skip count matched hyper's: the second head was seen as a head, so
    // the connection ends with the first answer and the second is never parsed.
    let (head, _) = read_response(&mut stream).await.unwrap();
    assert_eq!(status(&head), 200);
    assert!(closes(&head));
    assert!(ended(&mut stream).await);
    assert_eq!(*running.probe.seen.lock().unwrap(), ["/read"]);
}

#[tokio::test]
async fn conflicting_framing_is_a_400_before_any_read() {
    let running = start().await;
    let cases = [
        (
            "length then coding",
            "content-length: 2\r\ntransfer-encoding: chunked",
        ),
        (
            "coding then length",
            "transfer-encoding: chunked\r\ncontent-length: 2",
        ),
        ("repeated length", "content-length: 2\r\ncontent-length: 2"),
        ("gzip", "transfer-encoding: gzip"),
        ("gzip, chunked", "transfer-encoding: gzip, chunked"),
        ("chunked, gzip", "transfer-encoding: chunked, gzip"),
        ("identity", "transfer-encoding: identity"),
    ];
    for (name, headers) in cases {
        let request = format!("{POST}{headers}\r\n\r\n2\r\nab\r\n0\r\n\r\n");
        let (code, _, ended) = exchange(&running, request.as_bytes()).await;
        assert_eq!(code, 400, "{name}");
        assert!(ended, "{name}");
    }
    assert!(running.probe.seen.lock().unwrap().is_empty());
}

#[tokio::test]
async fn an_unread_body_closes_and_the_leftover_is_never_parsed() {
    let running = start().await;
    let mut stream = connect(&running).await;
    let request = "POST /unread HTTP/1.1\r\nhost: t\r\ncontent-length: 40\r\n\r\n\
                   GET /smuggled HTTP/1.1\r\nhost: t\r\n\r\n";
    stream.write_all(request.as_bytes()).await.unwrap();
    let (head, _) = read_response(&mut stream).await.unwrap();
    assert_eq!(status(&head), 200);
    assert!(closes(&head));
    assert!(ended(&mut stream).await);
    assert_eq!(*running.probe.seen.lock().unwrap(), ["/unread"]);
}

#[tokio::test]
async fn a_refused_body_is_not_parsed_as_the_next_request_either() {
    let running = start().await;
    let mut stream = connect(&running).await;
    let request = format!(
        "{POST}content-length: 2\r\ntransfer-encoding: chunked\r\n\r\n\
         GET /smuggled HTTP/1.1\r\nhost: t\r\n\r\n"
    );
    stream.write_all(request.as_bytes()).await.unwrap();
    let (head, _) = read_response(&mut stream).await.unwrap();
    assert_eq!(status(&head), 400);
    assert!(ended(&mut stream).await);
    assert!(running.probe.seen.lock().unwrap().is_empty());
}

#[tokio::test]
async fn dropping_a_held_body_lets_the_connection_end() {
    let running = start().await;
    let mut stream = connect(&running).await;
    let request = "POST /hold HTTP/1.1\r\nhost: t\r\ncontent-length: 40\r\n\r\n\
                   GET /smuggled HTTP/1.1\r\nhost: t\r\n\r\n";
    stream.write_all(request.as_bytes()).await.unwrap();
    let (head, _) = read_response(&mut stream).await.unwrap();
    assert!(closes(&head));
    // A backend that keeps the unread body keeps hyper from finishing the
    // connection: it must drop it when it answers (the bindings do in `respond`).
    running.probe.held.lock().unwrap().clear();
    assert!(timeout(Duration::from_secs(1), ended(&mut stream))
        .await
        .expect("the connection ends once the body is dropped"));
    assert_eq!(*running.probe.seen.lock().unwrap(), ["/hold"]);
}

#[tokio::test]
async fn continue_is_sent_only_when_the_body_is_read() {
    let running = start().await;
    // Read: 100 first, then the answer once the body is sent.
    let mut stream = connect(&running).await;
    let head = format!("{POST}content-length: 3\r\nexpect: 100-continue\r\n\r\n");
    stream.write_all(head.as_bytes()).await.unwrap();
    let (interim, _) = read_response(&mut stream).await.unwrap();
    assert_eq!(status(&interim), 100);
    stream.write_all(b"abc").await.unwrap();
    let (head, body) = read_response(&mut stream).await.unwrap();
    assert_eq!((status(&head), body.as_slice()), (200, &b"abc"[..]));
    // Not read: no 100, straight to the answer.
    let mut stream = connect(&running).await;
    let head =
        "POST /unread HTTP/1.1\r\nhost: t\r\ncontent-length: 3\r\nexpect: 100-continue\r\n\r\n";
    stream.write_all(head.as_bytes()).await.unwrap();
    let (head, _) = read_response(&mut stream).await.unwrap();
    assert_eq!(status(&head), 200);
    // Declared over the limit: refused without a 100.
    let mut stream = connect(&running).await;
    let head = format!("{POST}content-length: 9999\r\nexpect: 100-continue\r\n\r\n");
    stream.write_all(head.as_bytes()).await.unwrap();
    let (head, _) = read_response(&mut stream).await.unwrap();
    assert_eq!(status(&head), 413);
}

#[tokio::test]
async fn malformed_framing_never_reaches_a_success_answer() {
    let running = start().await;
    let cases: [(&str, String); 8] = [
        (
            "differing duplicate length",
            format!("{POST}content-length: 2\r\ncontent-length: 3\r\n\r\nabc"),
        ),
        (
            "length list",
            format!("{POST}content-length: 2, 3\r\n\r\nab"),
        ),
        (
            "negative length",
            format!("{POST}content-length: -2\r\n\r\nab"),
        ),
        (
            "chunk size overflow",
            format!(
                "{POST}transfer-encoding: chunked\r\n\r\nffffffffffffffffffff\r\nab\r\n0\r\n\r\n"
            ),
        ),
        (
            "bad chunk size",
            format!("{POST}transfer-encoding: chunked\r\n\r\nzz\r\nab\r\n0\r\n\r\n"),
        ),
        (
            "bare LF in a chunk size line",
            format!("{POST}transfer-encoding: chunked\r\n\r\n2\nab\r\n0\r\n\r\n"),
        ),
        (
            "bare LF in a trailer",
            format!("{POST}transfer-encoding: chunked\r\n\r\n2\r\nab\r\n0\r\nx: y\n\r\n"),
        ),
        (
            "space before the colon",
            format!("{POST}content-length : 2\r\n\r\nab"),
        ),
    ];
    for (name, request) in &cases {
        let mut stream = connect(&running).await;
        stream.write_all(request.as_bytes()).await.unwrap();
        let answer = read_response(&mut stream).await;
        let code = answer.map(|(head, _)| status(&head));
        eprintln!("{name}: {code:?}");
        assert!(matches!(code, None | Some(400 | 408)), "{name}: {code:?}");
        assert!(ended(&mut stream).await, "{name}");
    }
}

/// hyper's buffer plus the request line margin: the head bound both layers use.
const HEAD_LIMIT: usize = 32_768 + 8 * 1024;

/// `prefix`, a run of `a` and `rest`, such that the head (up to and including
/// the blank line, which ends `rest` or is followed by `tail`) is `total` bytes.
fn padded(prefix: &str, rest: &str, tail: &str, total: usize) -> Vec<u8> {
    let fixed = prefix.len() + rest.len() + tail.len();
    format!("{prefix}{}{rest}{tail}", "a".repeat(total - fixed)).into_bytes()
}

const BARE_LF: (&str, &str) = (
    " HTTP/1.1\r\nhost: t\r\nx: a\ncontent-length: 5\r\n\r\n",
    "hello",
);
const CL_TE: (&str, &str) = (
    " HTTP/1.1\r\nhost: t\r\ncontent-length: 4\r\ntransfer-encoding: chunked\r\n\r\n",
    "0\r\n\r\nGET /wrapped HTTP/1.1\r\nhost: t\r\n\r\n",
);

/// Send `request` cut at `splits` (a pause between the pieces); report the
/// status of the first answer, if any.
async fn attempt(running: &Running, request: &[u8], splits: &[usize]) -> Option<u16> {
    let mut stream = connect(running).await;
    let mut from = 0;
    for &at in splits.iter().chain([&request.len()]) {
        if stream.write_all(&request[from..at]).await.is_err() {
            break;
        }
        from = at;
        tokio::time::sleep(Duration::from_millis(30)).await;
    }
    read_response(&mut stream)
        .await
        .map(|(head, _)| status(&head))
}

#[tokio::test]
async fn an_oversized_head_never_hides_smuggling_from_the_guard() {
    let running = start().await;
    for size in [HEAD_LIMIT + 1, 41_000, 45_000, 48_000] {
        for (rest, tail) in [BARE_LF, CL_TE] {
            let head = padded("POST /p/", rest, "", size);
            let request = [head, tail.as_bytes().to_vec()].concat();
            let code = attempt(&running, &request, &[]).await;
            assert!(matches!(code, None | Some(400 | 431)), "{size}: {code:?}");
        }
    }
    assert!(running.probe.seen.lock().unwrap().is_empty());
}

#[tokio::test]
async fn the_same_attack_split_at_the_bound_fails_closed() {
    let running = start().await;
    for total in [HEAD_LIMIT - 1, HEAD_LIMIT, HEAD_LIMIT + 1] {
        for (rest, tail) in [BARE_LF, CL_TE] {
            let head = padded("POST /p/", rest, "", total);
            let request = [head, tail.as_bytes().to_vec()].concat();
            for cut in [HEAD_LIMIT - 1, HEAD_LIMIT, HEAD_LIMIT + 1] {
                let code = attempt(&running, &request, &[cut]).await;
                assert!(
                    matches!(code, None | Some(400 | 431)),
                    "{total} cut at {cut}: {code:?}"
                );
            }
        }
    }
    assert!(running.probe.seen.lock().unwrap().is_empty());
}

#[tokio::test]
async fn a_head_at_the_bound_is_served_and_one_byte_more_is_not() {
    let running = start().await;
    let tail = "\r\n\r\n";
    let at = padded("GET /", " HTTP/1.1\r\nhost: t", tail, HEAD_LIMIT);
    assert_eq!(attempt(&running, &at, &[]).await, Some(200));
    let split = attempt(&running, &at, &[HEAD_LIMIT - 1]).await;
    assert_eq!(split, Some(200));
    let over = padded("GET /", " HTTP/1.1\r\nhost: t", tail, HEAD_LIMIT + 1);
    let code = attempt(&running, &over, &[]).await;
    assert!(matches!(code, None | Some(400 | 431)), "{code:?}");
}
