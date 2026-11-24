#![no_main]

use std::sync::atomic::Ordering;
use std::sync::Arc;

use kiso::framing::{Framing, Signals};
use libfuzzer_sys::fuzz_target;

const HEAD_END: &[u8] = b"\r\n\r\n";
const HEADER_SLOTS: usize = 128;

fn trim_ows(value: &[u8]) -> &[u8] {
    let is_ows = |byte: &u8| matches!(byte, b' ' | b'\t');
    let start = value
        .iter()
        .position(|byte| !is_ows(byte))
        .unwrap_or(value.len());
    let end = value
        .iter()
        .rposition(|byte| !is_ows(byte))
        .map_or(start, |index| index + 1);
    &value[start..end]
}

fn framed(input: &[u8], max_head: usize, chunk_size: usize) -> (Vec<u8>, bool, bool) {
    let mut bytes = input.to_vec();
    let signals = Arc::new(Signals::default());
    let mut framing = Framing::new(max_head, Arc::clone(&signals));
    for chunk in bytes.chunks_mut(chunk_size) {
        framing.feed(chunk);
    }
    (
        bytes,
        signals.close.load(Ordering::Acquire),
        signals.linger.load(Ordering::Acquire),
    )
}

fn parse_head(bytes: &[u8]) -> Option<(usize, Vec<httparse::Header<'_>>)> {
    let mut headers = vec![httparse::EMPTY_HEADER; HEADER_SLOTS];
    let mut request = httparse::Request::new(&mut headers);
    match request.parse(bytes) {
        Ok(httparse::Status::Complete(head_len)) => Some((head_len, request.headers.to_vec())),
        Ok(httparse::Status::Partial) | Err(_) => None,
    }
}

fn assert_head_safety(
    bytes: &[u8],
    head_len: usize,
    headers: &[httparse::Header<'_>],
    max_head: usize,
) {
    assert!(head_len <= max_head);
    assert!(bytes[..head_len]
        .iter()
        .enumerate()
        .all(|(index, byte)| { *byte != b'\n' || (index > 0 && bytes[index - 1] == b'\r') }));

    let mut content_lengths = 0;
    let mut transfer_encodings = 0;
    for header in headers {
        let value = trim_ows(header.value);
        if header.name.eq_ignore_ascii_case("content-length") {
            content_lengths += 1;
            assert!(!value.is_empty());
            assert!(value.iter().all(u8::is_ascii_digit));
            assert!(std::str::from_utf8(value)
                .ok()
                .and_then(|value| value.parse::<u64>().ok())
                .is_some());
        } else if header.name.eq_ignore_ascii_case("transfer-encoding") {
            transfer_encodings += 1;
            assert!(value.eq_ignore_ascii_case(b"chunked"));
        }
    }
    assert!(content_lengths <= 1);
    assert!(transfer_encodings <= 1);
    assert!(!(content_lengths > 0 && transfer_encodings > 0));
}

fn assert_body_opaque(
    original: &[u8],
    guarded: &[u8],
    head_len: usize,
    headers: &[httparse::Header<'_>],
) {
    let mut content_length = None;
    let mut transfer_encoding = false;
    for header in headers {
        if header.name.eq_ignore_ascii_case("content-length") {
            content_length = std::str::from_utf8(trim_ows(header.value))
                .ok()
                .and_then(|value| value.parse::<usize>().ok());
        }
        if header.name.eq_ignore_ascii_case("transfer-encoding") {
            transfer_encoding = true;
        }
    }
    if let (Some(length), false) = (content_length, transfer_encoding) {
        if let Some(end) = head_len
            .checked_add(length)
            .filter(|end| *end <= original.len())
        {
            assert_eq!(&guarded[head_len..end], &original[head_len..end]);
        }
    }
}

fn delimiter_within(bytes: &[u8], max_head: usize) -> bool {
    bytes
        .windows(HEAD_END.len())
        .position(|window| window == HEAD_END)
        .is_some_and(|start| start + HEAD_END.len() <= max_head)
}

fuzz_target!(|input: &[u8]| {
    if input.len() < 3 {
        return;
    }
    let max_head = usize::from(u16::from_le_bytes([input[0], input[1]])) % 512 + 1;
    let chunk_size = usize::from(input[2]) % 255 + 1;
    let payload = &input[3..];
    let (whole, whole_close, whole_linger) = framed(payload, max_head, payload.len().max(1));
    let (chunked, chunk_close, chunk_linger) = framed(payload, max_head, chunk_size);
    assert_eq!(whole, chunked);
    assert_eq!(whole_close, chunk_close);
    assert_eq!(whole_linger, chunk_linger);

    if payload.len() > max_head && !delimiter_within(payload, max_head) {
        assert!(whole_close);
        assert!(whole_linger);
        assert!(whole[..=max_head].contains(&0));
    }

    if let Some((head_len, headers)) = parse_head(&whole) {
        assert_head_safety(&whole, head_len, &headers, max_head);
        assert_body_opaque(payload, &whole, head_len, &headers);
    }
});
