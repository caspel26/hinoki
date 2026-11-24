//! Request size limits, with the same defaults and formulas as the Python
//! runtime (`RuntimeConfig`, `core.request_headers`).

use crate::early::Early;

/// Overhead counted per header besides name and value (`": "` and CRLF).
pub const HEADER_OVERHEAD: usize = 4;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Limits {
    pub max_query_bytes: usize,
    pub max_header_bytes: usize,
    pub max_body_bytes: usize,
    pub max_in_flight: usize,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            max_query_bytes: 8192,
            max_header_bytes: 32_768,
            max_body_bytes: 1_048_576,
            max_in_flight: 288,
        }
    }
}

/// Bytes the header limit counts: name + value + [`HEADER_OVERHEAD`] each.
pub fn header_total<'a, I>(headers: I) -> usize
where
    I: IntoIterator<Item = (&'a [u8], &'a [u8])>,
{
    headers.into_iter().fold(0usize, |total, (name, value)| {
        add_header_total(total, name.len(), value.len())
    })
}

fn add_header_total(total: usize, name_len: usize, value_len: usize) -> usize {
    total
        .saturating_add(name_len)
        .saturating_add(value_len)
        .saturating_add(HEADER_OVERHEAD)
}

impl Limits {
    /// The early answer for an oversized query or header block, if any. The
    /// query is checked first, like the Python pipeline.
    pub fn check_request(&self, query_len: usize, header_total: usize) -> Option<Early> {
        if query_len > self.max_query_bytes {
            return Some(Early::UriTooLong);
        }
        if header_total > self.max_header_bytes {
            return Some(Early::HeadersTooLarge);
        }
        None
    }

    /// Whether a declared `Content-Length` is above the body limit. A value that
    /// is not plain ASCII digits is not judged here (the body reader decides).
    pub fn declared_too_large(&self, declared: &[u8]) -> bool {
        if declared.is_empty() || !declared.iter().all(u8::is_ascii_digit) {
            return false;
        }
        // Digits only: an overflowing number is certainly above any limit.
        let text = std::str::from_utf8(declared).unwrap_or("0");
        text.parse::<usize>()
            .map_or(true, |n| n > self.max_body_bytes)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn header_total_counts_overhead() {
        let headers: [(&[u8], &[u8]); 2] = [(b"a", b"bb"), (b"ccc", b"")];
        assert_eq!(header_total(headers), (1 + 2 + 4) + (3 + 4));
    }

    #[test]
    fn query_is_checked_before_headers() {
        let limits = Limits::default();
        assert_eq!(limits.check_request(8193, 99_999), Some(Early::UriTooLong));
        assert_eq!(
            limits.check_request(8192, 32_769),
            Some(Early::HeadersTooLarge)
        );
        assert_eq!(limits.check_request(8192, 32_768), None);
    }

    #[test]
    fn declared_length_rules() {
        let limits = Limits::default();
        assert!(!limits.declared_too_large(b"1048576"));
        assert!(limits.declared_too_large(b"1048577"));
        assert!(limits.declared_too_large(b"99999999999999999999999999"));
        assert!(!limits.declared_too_large(b""));
        assert!(!limits.declared_too_large(b"-1"));
        assert!(!limits.declared_too_large(b"abc"));
    }
}
