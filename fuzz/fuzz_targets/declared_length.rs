#![no_main]

use kiso::Limits;
use libfuzzer_sys::fuzz_target;

// `Content-Length` is parsed before the body is read. Compare the limit check
// with an independent reading of the same rule.
fuzz_target!(|declared: &[u8]| {
    let limits = Limits::default();
    let digits = !declared.is_empty() && declared.iter().all(u8::is_ascii_digit);
    let expected = digits
        && std::str::from_utf8(declared)
            .ok()
            .and_then(|text| text.parse::<u128>().ok())
            .map_or(true, |value| value > limits.max_body_bytes as u128);
    assert_eq!(limits.declared_too_large(declared), expected);
});
