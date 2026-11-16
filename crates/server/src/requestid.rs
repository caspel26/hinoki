//! Request ids: validation of an inbound id and the generated format, the same
//! rules as `hinoki.runtime.proxy.inbound_request_id`.

pub const MAX_REQUEST_ID_BYTES: usize = 128;
const VISIBLE_ASCII_MIN: u8 = 0x21;
const VISIBLE_ASCII_MAX: u8 = 0x7E;
const COMMA: u8 = b',';

/// Accept exactly one bounded, visible-ASCII id without commas; anything else
/// (none, several, empty, too long, other bytes) is rejected.
pub fn inbound<'a>(values: &[&'a [u8]]) -> Option<&'a str> {
    let [value] = values else { return None };
    let ok = (1..=MAX_REQUEST_ID_BYTES).contains(&value.len())
        && value
            .iter()
            .all(|b| (VISIBLE_ASCII_MIN..=VISIBLE_ASCII_MAX).contains(b) && *b != COMMA);
    if ok {
        std::str::from_utf8(value).ok()
    } else {
        None
    }
}

/// `prefix` (16 hex, per worker) followed by `counter` as 16 hex digits.
pub fn generated(prefix: &str, counter: u64) -> String {
    format!("{prefix}{counter:016x}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_one_visible_value() {
        assert_eq!(inbound(&[b"abc-123"]), Some("abc-123"));
    }

    #[test]
    fn rejects_ambiguous_or_invalid() {
        assert_eq!(inbound(&[]), None);
        assert_eq!(inbound(&[b"a", b"b"]), None);
        assert_eq!(inbound(&[b""]), None);
        assert_eq!(inbound(&[b"a,b"]), None);
        assert_eq!(inbound(&[b"a b"]), None);
        assert_eq!(inbound(&["é".as_bytes()]), None);
        assert_eq!(inbound(&[&[b'a'; 129]]), None);
        assert!(inbound(&[&[b'a'; 128]]).is_some());
    }

    #[test]
    fn generated_format() {
        assert_eq!(
            generated("0123456789abcdef", 255),
            "0123456789abcdef00000000000000ff"
        );
    }
}
