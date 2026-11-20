#![no_main]

use libfuzzer_sys::fuzz_target;

// An allowed `Host` header must really name an allowed host.
fuzz_target!(|header: &[u8]| {
    let hosts = kiso::hosts::Hosts {
        exact: vec![b"example.com".to_vec()],
        suffixes: vec![b".example.com".to_vec()],
    };
    if hosts.allows(header) {
        let lowered = header.to_ascii_lowercase();
        assert!(lowered.windows(11).any(|window| window == b"example.com"));
    }
});
