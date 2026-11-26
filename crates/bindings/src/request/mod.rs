//! Request decoding and JSON, in Rust: query strings, cookies, repeated
//! headers, JSON into Python objects and Python objects into JSON bytes.
//!
//! The pure functions have no Python dependency and carry the unit tests; the
//! `#[pyfunction]`s only convert between Python and them. The ASGI scope itself
//! is still a Python object, so this removes the parsing and serialising work
//! from Python, not the crossing.

mod json;
mod multipart;
mod parse;
mod pyapi;

#[cfg(test)]
pub(crate) use self::json::json_to_python;
pub use self::json::{decode_json, encode_json, JsonDecodeError};
pub(crate) use self::json::{
    decode_json_document, encode_json_bytes, encode_json_bytes_strict, float_text, int_text,
    is_instance_of_named, key_text, push_i64, push_json_string, push_u64, string_bytes, Json,
    JsonObject, MAX_DEPTH,
};
#[allow(unused_imports)]
pub use self::multipart::{
    negotiate_media, parse_multipart, parse_multipart_body, MultipartError, MultipartLimitError,
    Part,
};
#[allow(unused_imports)]
pub use self::parse::{
    decode_path_parameter, join_headers, lookup_header, negotiate, parse_cookies, parse_query,
};
#[allow(unused_imports)]
pub use self::pyapi::{
    header_dict, headers_and_size, joined_header_dict, parse_cookie_header, parse_query_string,
    request_headers, response_headers,
};

#[cfg(test)]
mod tests {
    use super::*;

    fn pair(a: &str, b: &str) -> (String, String) {
        (a.to_owned(), b.to_owned())
    }

    fn offered(types: &[&str]) -> Vec<String> {
        types.iter().map(|t| (*t).to_owned()).collect()
    }

    #[test]
    fn review_regressions() {
        assert!(parse_query(b"a=1&b=2&c=3", 2).is_err());
        assert!(parse_query(b"a=1&b=2", 2).is_ok());
        let split = join_headers(&[
            (b"Cookie".to_vec(), b"a=1".to_vec()),
            (b"cookie".to_vec(), b"session=abc".to_vec()),
        ]);
        assert_eq!(split, vec![pair("cookie", "a=1; session=abc")]);
        let csv = offered(&["text/csv; charset=utf-8"]);
        assert_eq!(negotiate("text/csv", &csv), Some(0));
        let json = offered(&["application/json"]);
        assert_eq!(
            negotiate("application/json, application/json;q=0", &json),
            Some(0)
        );
        let head = |h: &str| format!("--B\r\n{h}\r\n\r\nx\r\n--B--");
        let parse = |h: &str| parse_multipart(head(h).as_bytes(), "B", 5, 9);
        assert!(parse("Content-Disposition: Form-Data; name=\"a\"").is_ok());
        assert!(parse("Content-Disposition: form-data; name=\"a\"; name=\"b\"").is_err());
        assert!(parse("Content-Disposition: form-data; name=\"a\"; filename=\"x\ny\"").is_err());
        assert!(parse("Content-Disposition: form-data; name=\"a\"\nX: y").is_err());
    }

    #[test]
    fn lookup_header_matches_join_headers_semantics() {
        let headers = vec![
            (b"Host".to_vec(), b"example.com".to_vec()),
            (b"Cookie".to_vec(), b"a=1".to_vec()),
            (b"cookie".to_vec(), b"b=2".to_vec()),
            (b"Authorization".to_vec(), b"Bearer one".to_vec()),
            (b"authorization".to_vec(), b"Bearer two".to_vec()),
        ];
        assert_eq!(
            lookup_header(&headers, "host").as_deref(),
            Some("example.com")
        );
        assert_eq!(lookup_header(&headers, "Host"), None);
        assert_eq!(
            lookup_header(&headers, "cookie").as_deref(),
            Some("a=1; b=2")
        );
        assert_eq!(
            lookup_header(&headers, "authorization").as_deref(),
            Some("Bearer one, Bearer two")
        );
        assert_eq!(lookup_header(&headers, "x-missing"), None);

        let latin1 = vec![(b"X-Note".to_vec(), vec![b'a', 0xe9])];
        assert_eq!(lookup_header(&latin1, "x-note").as_deref(), Some("a\u{e9}"));
    }
}
