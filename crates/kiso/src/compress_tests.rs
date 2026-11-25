//! Compression: the decision per answer, the negotiation and the encoded body.

use std::future::poll_fn;
use std::io::Read;
use std::time::Duration;

use bytes::Bytes;
use http_body_util::Full;
use tokio::sync::mpsc;

use crate::compress::{negotiate, whole_bytes, Compression, Config, Shape};
use crate::encode::Coding;
use crate::headers::Header;
use crate::stream::{Aborted, Chunk, Live, Pace, Reply};

fn header(name: &str, value: &str) -> Header {
    (name.as_bytes().to_vec(), value.as_bytes().to_vec())
}

fn policy(change: impl FnOnce(&mut Config)) -> std::sync::Arc<Compression> {
    let mut config = Config::default();
    change(&mut config);
    std::sync::Arc::new(Compression::new(config).unwrap())
}

fn text(length: usize) -> Vec<u8> {
    b"hello compression "
        .iter()
        .copied()
        .cycle()
        .take(length)
        .collect()
}

fn json() -> Vec<Header> {
    vec![header("content-type", "application/json; charset=utf-8")]
}

/// Run a whole answer through the policy: its final headers and body bytes.
fn through(
    policy: &std::sync::Arc<Compression>,
    accept: &str,
    mut headers: Vec<Header>,
    body: &[u8],
    head_only: bool,
) -> (Vec<Header>, Vec<u8>) {
    headers.push(header("content-length", &body.len().to_string()));
    let offer = policy.offer(&[header("accept-encoding", accept)]);
    let reply = offer.apply(
        200,
        &mut headers,
        Reply::Whole(Full::new(Bytes::from(body.to_vec()))),
        Shape::Whole(body.len() as u64),
        head_only,
    );
    let Reply::Whole(full) = reply else {
        panic!("a whole body stays whole at this size");
    };
    (headers, whole_bytes(full).to_vec())
}

fn value<'a>(headers: &'a [Header], name: &str) -> Option<&'a str> {
    headers
        .iter()
        .find(|(candidate, _)| candidate.eq_ignore_ascii_case(name.as_bytes()))
        .map(|(_, value)| std::str::from_utf8(value).unwrap())
}

fn gunzip(bytes: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    flate2::read::GzDecoder::new(bytes)
        .read_to_end(&mut out)
        .unwrap();
    out
}

fn unbrotli(bytes: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    brotli::Decompressor::new(bytes, 4096)
        .read_to_end(&mut out)
        .unwrap();
    out
}

#[test]
fn negotiation_follows_q_values_and_the_servers_order() {
    let both = [Coding::Brotli, Coding::Gzip];
    let pick = |accept: &str| negotiate(accept.as_bytes(), &both);
    assert_eq!(pick("gzip, br"), Some(Coding::Brotli));
    assert_eq!(pick("gzip;q=1, br;q=0.5"), Some(Coding::Gzip));
    assert_eq!(pick("br;q=0, gzip"), Some(Coding::Gzip));
    assert_eq!(pick("x-gzip"), Some(Coding::Gzip));
    assert_eq!(pick("*"), Some(Coding::Brotli));
    assert_eq!(pick("*;q=0.1, gzip"), Some(Coding::Gzip));
    assert_eq!(pick("GZIP;Q=0.8"), Some(Coding::Gzip));
    assert_eq!(pick("gzip;q=1.000"), Some(Coding::Gzip));
}

#[test]
fn nothing_acceptable_negotiates_no_coding() {
    let both = [Coding::Brotli, Coding::Gzip];
    for accept in [
        "",
        "identity",
        "deflate",
        "*;q=0",
        "gzip;q=0, br;q=0",
        "br;q=x",
        "gzip;q=2",
    ] {
        assert_eq!(negotiate(accept.as_bytes(), &both), None, "{accept:?}");
    }
    assert_eq!(negotiate(b"br", &[Coding::Gzip]), None);
}

#[test]
fn only_the_first_entries_of_accept_encoding_are_read() {
    let filler = "a, ".repeat(32);
    let accept = format!("{filler}gzip");
    assert_eq!(negotiate(accept.as_bytes(), &[Coding::Gzip]), None);
}

#[test]
fn a_compressible_body_is_encoded_with_its_new_length() {
    let compression = policy(|_| {});
    let body = text(5000);
    let (headers, sent) = through(&compression, "br, gzip", json(), &body, false);
    assert_eq!(value(&headers, "content-encoding"), Some("br"));
    assert_eq!(value(&headers, "vary"), Some("accept-encoding"));
    assert_eq!(
        value(&headers, "content-length"),
        Some(&*sent.len().to_string())
    );
    assert!(sent.len() < body.len());
    assert_eq!(unbrotli(&sent), body);

    let (headers, sent) = through(&compression, "gzip", json(), &body, false);
    assert_eq!(value(&headers, "content-encoding"), Some("gzip"));
    assert_eq!(gunzip(&sent), body);
}

#[test]
fn a_body_that_does_not_shrink_goes_out_as_it_was() {
    let compression = policy(|config| config.min_size = 1);
    let (headers, sent) = through(&compression, "gzip", json(), b"x", false);
    assert_eq!(sent, b"x");
    assert_eq!(value(&headers, "content-encoding"), None);
    assert_eq!(value(&headers, "content-length"), Some("1"));
    assert_eq!(value(&headers, "vary"), Some("accept-encoding"));
}

#[test]
fn an_answer_that_cannot_vary_is_left_alone_without_vary() {
    let compression = policy(|_| {});
    let body = text(5000);
    let untouched = |headers: Vec<Header>, body: &[u8]| {
        let (out, sent) = through(&compression, "gzip", headers, body, false);
        assert_eq!(sent, body);
        assert_eq!(value(&out, "content-encoding"), None);
        assert_eq!(value(&out, "vary"), None);
    };
    untouched(vec![], &body);
    untouched(vec![header("content-type", "image/png")], &body);
    untouched(vec![header("content-type", "text/event-stream")], &body);
    untouched(json(), &text(100));
    untouched(json(), b"");
}

#[test]
fn an_answer_the_app_already_shaped_is_left_alone() {
    let compression = policy(|_| {});
    let body = text(5000);
    for extra in [
        header("content-encoding", "br"),
        header("cache-control", "private, No-Transform"),
        header("content-range", "bytes 0-4999/9999"),
    ] {
        let mut headers = json();
        headers.push(extra);
        let (out, sent) = through(&compression, "gzip", headers, &body, false);
        assert_eq!(sent, body);
        assert_eq!(value(&out, "vary"), None);
    }
}

#[test]
fn vary_is_added_once_and_not_to_a_vary_star() {
    let compression = policy(|_| {});
    let body = text(5000);
    let vary = |existing: &str| {
        let mut headers = json();
        headers.push(header("vary", existing));
        let (out, _) = through(&compression, "gzip", headers, &body, false);
        out.iter().filter(|(name, _)| name == b"vary").count()
    };
    assert_eq!(vary("origin"), 2);
    assert_eq!(vary("Origin, Accept-Encoding"), 1);
    assert_eq!(vary("*"), 1);
}

#[test]
fn a_client_without_a_usable_coding_gets_the_body_with_vary() {
    let compression = policy(|_| {});
    let body = text(5000);
    for accept in ["", "identity", "deflate"] {
        let (headers, sent) = through(&compression, accept, json(), &body, false);
        assert_eq!(sent, body);
        assert_eq!(value(&headers, "content-encoding"), None);
        assert_eq!(value(&headers, "vary"), Some("accept-encoding"));
    }
}

#[test]
fn only_the_allowed_codings_and_types_apply() {
    let compression = policy(|config| {
        config.codings = vec![Coding::Gzip];
        config.types = vec!["text/*".into(), "*/*+json".into()];
    });
    let body = text(5000);
    let (headers, _) = through(&compression, "br, gzip", json(), &body, false);
    assert_eq!(value(&headers, "content-encoding"), None);
    let vendor = vec![header("content-type", "application/vnd.api+json")];
    let (headers, _) = through(&compression, "br, gzip", vendor, &body, false);
    assert_eq!(value(&headers, "content-encoding"), Some("gzip"));
}

#[test]
fn a_strong_etag_becomes_weak_and_head_keeps_the_headers_without_the_length() {
    let compression = policy(|_| {});
    let mut headers = json();
    headers.push(header("etag", "\"abc\""));
    let (out, _) = through(&compression, "gzip", headers.clone(), &text(5000), false);
    assert_eq!(value(&out, "etag"), Some("W/\"abc\""));

    let (out, sent) = through(&compression, "gzip", headers, &text(5000), true);
    assert_eq!(value(&out, "content-encoding"), Some("gzip"));
    assert_eq!(value(&out, "content-length"), None);
    assert_eq!(value(&out, "vary"), Some("accept-encoding"));
    assert_eq!(value(&out, "etag"), Some("W/\"abc\""));
    assert_eq!(
        sent,
        text(5000),
        "HEAD hands the body over untouched for the transport to drop"
    );
}

#[tokio::test]
async fn a_large_whole_body_is_encoded_in_pieces_without_a_length() {
    let compression = policy(|_| {});
    let body = text(300_000);
    let mut headers = json();
    headers.push(header("content-length", "300000"));
    let offer = compression.offer(&[header("accept-encoding", "gzip")]);
    let reply = offer.apply(
        200,
        &mut headers,
        Reply::Whole(Full::new(Bytes::from(body.clone()))),
        Shape::Whole(300_000),
        false,
    );
    assert_eq!(value(&headers, "content-length"), None);
    assert_eq!(value(&headers, "content-encoding"), Some("gzip"));
    let Reply::Encoded(mut encoded) = reply else {
        panic!("a large body is encoded in pieces");
    };
    let mut sent = Vec::new();
    while let Some(piece) = poll_fn(|cx| encoded.poll_piece(cx, 16 * 1024)).await {
        let piece = piece.unwrap();
        assert!(piece.len() <= 16 * 1024);
        sent.extend_from_slice(&piece);
    }
    assert_eq!(gunzip(&sent), body);
}

fn live_reply(
    compression: &std::sync::Arc<Compression>,
    accept: &str,
    headers: Vec<Header>,
    declared: Option<u64>,
    pace: Pace,
) -> (mpsc::Sender<Chunk>, Option<Reply>, Vec<Header>) {
    let (sender, receiver) = mpsc::channel(4);
    let mut headers = headers;
    let live = Live::with_length(receiver, declared).paced(pace);
    let offer = compression.offer(&[header("accept-encoding", accept)]);
    let reply = offer.apply(
        200,
        &mut headers,
        Reply::Live(live),
        Shape::Live(declared),
        false,
    );
    (sender, Some(reply), headers)
}

/// What a decoder can read from the bytes received so far.
fn readable(coding: Coding, received: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    let mut buffer = [0u8; 256];
    let mut reader: Box<dyn Read + '_> = match coding {
        Coding::Gzip => Box::new(flate2::read::GzDecoder::new(received)),
        Coding::Brotli => Box::new(brotli::Decompressor::new(received, 4096)),
    };
    while let Ok(read) = reader.read(&mut buffer) {
        if read == 0 {
            break;
        }
        out.extend_from_slice(&buffer[..read]);
    }
    out
}

async fn next(encoded: &mut crate::encode::Encoded) -> Option<Result<Bytes, Aborted>> {
    poll_fn(|cx| encoded.poll_piece(cx, usize::MAX)).await
}

/// Receive output until the decoder can read `expected`.
async fn until_readable(
    encoded: &mut crate::encode::Encoded,
    coding: Coding,
    received: &mut Vec<u8>,
    expected: &[u8],
) {
    let waited = tokio::time::timeout(Duration::from_secs(5), async {
        while readable(coding, received) != expected {
            let piece = next(encoded).await.expect("the body goes on").unwrap();
            received.extend_from_slice(&piece);
        }
    })
    .await;
    assert!(waited.is_ok(), "the producer's piece was held back");
}

#[tokio::test]
async fn every_pause_of_the_producer_flushes_what_it_sent() {
    for (accept, coding) in [("gzip", Coding::Gzip), ("br", Coding::Brotli)] {
        let compression = policy(|_| {});
        let (sender, reply, _) = live_reply(&compression, accept, json(), None, Pace::default());
        let Some(Reply::Encoded(mut encoded)) = reply else {
            panic!("a stream is encoded");
        };
        let mut received = Vec::new();
        let mut expected = Vec::new();
        for part in [&b"first event\n"[..], b"second event\n"] {
            sender.send(Ok(Bytes::from(part.to_vec()))).await.unwrap();
            expected.extend_from_slice(part);
            until_readable(&mut encoded, coding, &mut received, &expected).await;
        }
        drop(sender);
        while let Some(piece) = next(&mut encoded).await {
            received.extend_from_slice(&piece.unwrap());
        }
        assert_eq!(readable(coding, &received), expected);
        assert!(encoded.is_done());
    }
}

#[tokio::test]
async fn keep_alive_comments_are_flushed_to_the_client() {
    let compression = policy(|config| config.event_streams = true);
    let headers = vec![header("content-type", "text/event-stream")];
    let pace = Pace {
        idle: None,
        keepalive: Some(Duration::from_millis(30)),
    };
    let (_sender, reply, _) = live_reply(&compression, "gzip", headers, None, pace);
    let Some(Reply::Encoded(mut encoded)) = reply else {
        panic!("an event stream is encoded when enabled");
    };
    let mut received = Vec::new();
    until_readable(
        &mut encoded,
        Coding::Gzip,
        &mut received,
        crate::sse::KEEPALIVE,
    )
    .await;
}

#[tokio::test]
async fn a_failed_producer_ends_the_body_without_a_valid_end() {
    let compression = policy(|_| {});
    let (sender, reply, _) = live_reply(&compression, "gzip", json(), None, Pace::default());
    let Some(Reply::Encoded(mut encoded)) = reply else {
        panic!("a stream is encoded");
    };
    sender
        .send(Ok(Bytes::from_static(b"partial")))
        .await
        .unwrap();
    sender.send(Err(Aborted)).await.unwrap();
    let mut received = Vec::new();
    let failure = loop {
        match next(&mut encoded).await {
            Some(Ok(piece)) => received.extend_from_slice(&piece),
            Some(Err(aborted)) => break aborted,
            None => panic!("a failed body must not end cleanly"),
        }
    };
    assert_eq!(failure, Aborted);
    let mut out = Vec::new();
    let complete = flate2::read::GzDecoder::new(&received[..]).read_to_end(&mut out);
    assert!(complete.is_err(), "the gzip trailer must be missing");
}

#[tokio::test]
async fn a_declared_length_is_still_checked_on_the_bytes_the_producer_sent() {
    let compression = policy(|config| config.min_size = 1);
    let (sender, reply, headers) =
        live_reply(&compression, "gzip", json(), Some(100), Pace::default());
    assert_eq!(value(&headers, "content-length"), None);
    let Some(Reply::Encoded(mut encoded)) = reply else {
        panic!("a stream is encoded");
    };
    sender.send(Ok(Bytes::from_static(b"short"))).await.unwrap();
    drop(sender);
    let outcome = loop {
        match next(&mut encoded).await {
            Some(Ok(_)) => {}
            other => break other,
        }
    };
    assert_eq!(outcome, Some(Err(Aborted)));
}

#[tokio::test]
async fn a_busy_policy_sends_the_answer_as_it_was() {
    let compression = policy(|config| config.max_concurrent = 1);
    let (_first, held, _) = live_reply(&compression, "gzip", json(), None, Pace::default());
    assert!(matches!(held, Some(Reply::Encoded(_))));
    let (_second, plain, headers) = live_reply(&compression, "gzip", json(), None, Pace::default());
    assert!(matches!(plain, Some(Reply::Live(_))));
    assert_eq!(value(&headers, "content-encoding"), None);
    assert_eq!(compression.busy(), 1);
    drop(held);
    let (_third, again, _) = live_reply(&compression, "gzip", json(), None, Pace::default());
    assert!(
        matches!(again, Some(Reply::Encoded(_))),
        "the slot came back"
    );
}

#[tokio::test]
async fn head_does_not_need_a_free_slot() {
    let compression = policy(|config| config.max_concurrent = 1);
    let (_first, held, _) = live_reply(&compression, "gzip", json(), None, Pace::default());
    assert!(matches!(held, Some(Reply::Encoded(_))));
    let (out, _) = through(&compression, "gzip", json(), &text(5000), true);
    assert_eq!(value(&out, "content-encoding"), Some("gzip"));
    assert_eq!(
        compression.busy(),
        0,
        "a HEAD takes no slot and counts none"
    );
}

#[test]
fn an_unusable_policy_is_refused() {
    let refused = |change: fn(&mut Config)| {
        let mut config = Config::default();
        change(&mut config);
        Compression::new(config).is_err()
    };
    assert!(refused(|c| c.codings.clear()));
    assert!(refused(|c| c.levels.gzip = 10));
    assert!(refused(|c| c.levels.brotli_quality = 10));
    assert!(!refused(|c| c.levels.brotli_quality = 9));
    assert!(refused(|c| c.levels.brotli_window = 9));
    assert!(refused(|c| c.max_concurrent = 0));
    assert!(refused(|c| c.types = vec!["json".into()]));
    assert!(refused(|c| c.types = vec!["*/json".into()]));
}

#[test]
fn a_head_answer_is_sized_by_the_length_it_declares() {
    use crate::conn::Answer;
    use crate::stream::Delivery;
    let answer = |length: &str| {
        Delivery::Whole(Answer {
            status: 200,
            headers: vec![header("content-length", length)],
            body: Bytes::new(),
        })
    };
    assert!(matches!(Shape::of(&answer("5000")), Shape::Whole(5000)));
    assert!(matches!(Shape::of(&answer("0")), Shape::Whole(0)));
}
