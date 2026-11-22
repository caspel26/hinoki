#![no_main]

use kiso::ws::{inspect, Upgrade};
use libfuzzer_sys::fuzz_target;

// The opening handshake is read from client headers: pairs split on the first
// NUL of each line. Whatever they hold it must not panic, and an offered key is
// always the 24 characters of a 16-byte nonce.
fuzz_target!(|input: &[u8]| {
    let headers: Vec<(Vec<u8>, Vec<u8>)> = input
        .split(|byte| *byte == b'\n')
        .filter_map(|line| {
            let at = line.iter().position(|byte| *byte == 0)?;
            Some((line[..at].to_ascii_lowercase(), line[at + 1..].to_vec()))
        })
        .collect();
    if let Upgrade::Offer(offer) = inspect("GET", &headers) {
        assert_eq!(offer.key.len(), 24);
        assert!(offer.protocols.iter().all(|name| !name.is_empty()));
    }
});
