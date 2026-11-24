#![no_main]

use std::sync::Arc;

use kiso::early::Early;
use kiso::framing::{Framing, Signals};
use kiso::limits::{header_total, Limits, HEADER_OVERHEAD};
use libfuzzer_sys::fuzz_target;

fn boundary_checks(query_len: usize, total: usize) {
    for query_limit in [0, query_len, query_len.saturating_sub(1)] {
        let limits = Limits {
            max_query_bytes: query_limit,
            max_header_bytes: 0,
            ..Limits::default()
        };
        assert_eq!(limits.check_request(query_limit, 0), None);
        if let Some(over) = query_limit.checked_add(1) {
            assert_eq!(limits.check_request(over, 0), Some(Early::UriTooLong));
        }
    }

    for header_limit in [0, total, total.saturating_sub(1)] {
        let limits = Limits {
            max_query_bytes: 0,
            max_header_bytes: header_limit,
            ..Limits::default()
        };
        assert_eq!(limits.check_request(0, header_limit), None);
        if let Some(over) = header_limit.checked_add(1) {
            assert_eq!(limits.check_request(0, over), Some(Early::HeadersTooLarge));
        }
    }

    let query_at_limit = Limits {
        max_query_bytes: query_len,
        max_header_bytes: total,
        ..Limits::default()
    };
    assert_eq!(query_at_limit.check_request(query_len, total), None);
    if query_len > 0 {
        let query_below_limit = Limits {
            max_query_bytes: query_len - 1,
            ..query_at_limit
        };
        assert_eq!(
            query_below_limit.check_request(query_len, total),
            Some(Early::UriTooLong)
        );
    }
    if total > 0 {
        let headers_below_limit = Limits {
            max_header_bytes: total - 1,
            ..query_at_limit
        };
        assert_eq!(
            headers_below_limit.check_request(query_len, total),
            Some(Early::HeadersTooLarge)
        );
    }
}

fn verify_request(bytes: &[u8], max_head: usize, max_headers: usize, limits: Limits) {
    let mut guarded = bytes.to_vec();
    let signals = Arc::new(Signals::default());
    Framing::new(max_head, Arc::clone(&signals)).feed(&mut guarded);

    let mut slots = vec![httparse::EMPTY_HEADER; max_headers];
    let mut request = httparse::Request::new(&mut slots);
    let Ok(httparse::Status::Complete(_)) = request.parse(&guarded) else {
        return;
    };
    assert!(request.headers.len() <= max_headers);

    let headers = request
        .headers
        .iter()
        .map(|header| (header.name.as_bytes(), header.value));
    let total = header_total(headers);
    let expected_total: u128 = request
        .headers
        .iter()
        .map(|header| {
            (header.name.len() as u128) + (header.value.len() as u128) + (HEADER_OVERHEAD as u128)
        })
        .sum();
    assert_eq!(total as u128, expected_total);

    let query_len = request
        .path
        .and_then(|path| path.split_once('?').map(|(_, query)| query.len()))
        .unwrap_or(0);
    let decision = limits.check_request(query_len, total);
    let expected = if query_len > limits.max_query_bytes {
        Some(Early::UriTooLong)
    } else if total > limits.max_header_bytes {
        Some(Early::HeadersTooLarge)
    } else {
        None
    };
    assert_eq!(decision, expected);
    boundary_checks(query_len, total);
}

fuzz_target!(|input: &[u8]| {
    if input.len() < 6 {
        return;
    }
    let max_head = usize::from(u16::from_le_bytes([input[0], input[1]])) % 512 + 1;
    let max_headers = usize::from(input[2]) % 32 + 1;
    let limits = Limits {
        max_query_bytes: usize::from(input[3]),
        max_header_bytes: usize::from(u16::from_le_bytes([input[4], input[5]])) % 512,
        ..Limits::default()
    };
    verify_request(&input[6..], max_head, max_headers, limits);
});
