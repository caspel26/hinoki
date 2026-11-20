#![no_main]

use libfuzzer_sys::fuzz_target;

// The host, path and query come straight from the client and end up in a
// `Location` header, so a redirect target must never carry a byte that could
// split the header or leave the configured host.
fuzz_target!(|input: (&[u8], &[u8], &[u8])| {
    let (host, path, query) = input;
    let Some(location) = kiso::redirect::location(host, path, query) else {
        return;
    };
    assert!(location.starts_with(b"https://"));
    assert!(location.iter().all(|byte| (0x21..0x7f).contains(byte)));
});
