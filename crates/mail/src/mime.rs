//! Body encodings: quoted-printable, base64 lines, RFC 2231 parameters, content
//! types and part boundaries. Everything produced is 7-bit ASCII with CRLF line
//! ends and lines of at most 76 characters.

use base64::engine::general_purpose::STANDARD;
use base64::Engine as _;

const QP_LINE: usize = 75;
const BASE64_LINE: usize = 76;
const MAX_CONTENT_TYPE: usize = 127;
/// Longest RFC 2231 encoded filename, so that its header line stays far below
/// the 998 octet limit.
pub(crate) const MAX_ENCODED_FILENAME: usize = 900;

/// `text` with every lone `\r` or `\n` turned into `\r\n`.
pub(crate) fn normalize_crlf(text: &str) -> String {
    let mut out = String::with_capacity(text.len() + text.len() / 16);
    let mut chars = text.chars().peekable();
    while let Some(ch) = chars.next() {
        match ch {
            '\r' => {
                chars.next_if_eq(&'\n');
                out.push_str("\r\n");
            }
            '\n' => out.push_str("\r\n"),
            other => out.push(other),
        }
    }
    out
}

fn push_hex(out: &mut String, byte: u8) {
    const HEX: &[u8; 16] = b"0123456789ABCDEF";
    out.push('=');
    out.push(char::from(HEX[usize::from(byte >> 4)]));
    out.push(char::from(HEX[usize::from(byte & 15)]));
}

/// Quoted-printable (RFC 2045) of CRLF-normalized `text`. Trailing blanks are
/// encoded, and lines are soft-broken so none exceeds 76 characters.
pub(crate) fn quoted_printable(text: &str) -> String {
    let mut out = String::with_capacity(text.len() + text.len() / 4);
    for (index, line) in text.split("\r\n").enumerate() {
        if index > 0 {
            out.push_str("\r\n");
        }
        encode_qp_line(line.as_bytes(), &mut out);
    }
    out
}

fn encode_qp_line(line: &[u8], out: &mut String) {
    let mut width = 0;
    for (position, &byte) in line.iter().enumerate() {
        let blank_at_end = matches!(byte, b' ' | b'\t') && position + 1 == line.len();
        let literal = (byte == b'\t' || byte == b' ' || (33..=126).contains(&byte))
            && byte != b'='
            && !blank_at_end;
        let size = if literal { 1 } else { 3 };
        if width + size > QP_LINE {
            out.push_str("=\r\n");
            width = 0;
        }
        if literal {
            out.push(char::from(byte));
        } else {
            push_hex(out, byte);
        }
        width += size;
    }
}

/// Standard base64 in lines of 76 characters, each ending in CRLF.
pub(crate) fn base64_lines(bytes: &[u8]) -> String {
    let encoded = STANDARD.encode(bytes);
    let mut out = String::with_capacity(encoded.len() + encoded.len() / BASE64_LINE * 2 + 2);
    for chunk in encoded.as_bytes().chunks(BASE64_LINE) {
        // Base64 output is ASCII, so every chunk is valid UTF-8.
        out.push_str(&String::from_utf8_lossy(chunk));
        out.push_str("\r\n");
    }
    out
}

fn is_token_byte(byte: u8) -> bool {
    (33..=126).contains(&byte) && !b"()<>@,;:\\\"/[]?=".contains(&byte)
}

/// `type/subtype` made of RFC 2045 tokens, at most 127 characters.
pub(crate) fn valid_content_type(value: &str) -> bool {
    let Some((kind, subtype)) = value.split_once('/') else {
        return false;
    };
    value.len() <= MAX_CONTENT_TYPE
        && [kind, subtype]
            .iter()
            .all(|part| !part.is_empty() && part.bytes().all(is_token_byte))
}

fn attribute_char(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || b"!#$&+-.^_`|~".contains(&byte)
}

/// The RFC 2231 value `utf-8''...` of `text`.
pub(crate) fn rfc2231_value(text: &str) -> String {
    let mut out = String::from("utf-8''");
    for &byte in text.as_bytes() {
        if attribute_char(byte) {
            out.push(char::from(byte));
        } else {
            out.push('%');
            let mut hex = String::new();
            push_hex(&mut hex, byte);
            out.push_str(&hex[1..]);
        }
    }
    out
}

fn quote_ascii(text: &str) -> String {
    let mut out = String::with_capacity(text.len() + 2);
    out.push('"');
    for ch in text.chars() {
        if matches!(ch, '"' | '\\') {
            out.push('\\');
        }
        out.push(ch);
    }
    out.push('"');
    out
}

/// The `filename` parameters of a disposition: a quoted ASCII fallback and, for
/// a name with other characters, the RFC 2231 `filename*` form.
pub(crate) fn filename_params(name: &str) -> Vec<String> {
    if name.is_ascii() {
        return vec![format!("filename={}", quote_ascii(name))];
    }
    let fallback: String = name
        .chars()
        .map(|ch| if ch.is_ascii() { ch } else { '_' })
        .collect();
    vec![
        format!("filename={}", quote_ascii(&fallback)),
        format!("filename*={}", rfc2231_value(name)),
    ]
}

/// A part boundary built from random bytes. The leading `=_` cannot appear in
/// quoted-printable output (`=` is always followed by two hex digits or a line
/// break) nor in base64.
pub(crate) fn boundary(random: &[u8; 12]) -> String {
    let mut out = String::from("=_hinoki_");
    for byte in random {
        out.push_str(&format!("{byte:02x}"));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn qp_decode(input: &str) -> Vec<u8> {
        let bytes = input.as_bytes();
        let mut out = Vec::new();
        let mut i = 0;
        while i < bytes.len() {
            if bytes[i] == b'=' {
                if bytes[i + 1] == b'\r' {
                    i += 3;
                } else {
                    let hex = std::str::from_utf8(&bytes[i + 1..i + 3]).unwrap();
                    out.push(u8::from_str_radix(hex, 16).unwrap());
                    i += 3;
                }
            } else {
                out.push(bytes[i]);
                i += 1;
            }
        }
        out
    }

    #[test]
    fn line_ends_are_normalized_to_crlf() {
        assert_eq!(
            normalize_crlf("a\nb\rc\r\nd\n\ne"),
            "a\r\nb\r\nc\r\nd\r\n\r\ne"
        );
        assert_eq!(normalize_crlf("\r\r\n"), "\r\n\r\n");
        assert_eq!(normalize_crlf("plain"), "plain");
    }

    #[test]
    fn quoted_printable_escapes_equals_non_ascii_and_trailing_blanks() {
        assert_eq!(quoted_printable("a=b"), "a=3Db");
        assert_eq!(quoted_printable("caffè"), "caff=C3=A8");
        assert_eq!(
            quoted_printable("end \r\nnext\t\r\nlast "),
            "end=20\r\nnext=09\r\nlast=20"
        );
        assert_eq!(quoted_printable("in side\tblank"), "in side\tblank");
        assert_eq!(quoted_printable(""), "");
        assert_eq!(quoted_printable("a\r\n\r\nb"), "a\r\n\r\nb");
    }

    #[test]
    fn quoted_printable_breaks_long_lines_and_round_trips() {
        for text in [
            "x".repeat(500),
            "è".repeat(120),
            format!("{}=", "y".repeat(74)),
            "z ".repeat(100),
        ] {
            let encoded = quoted_printable(&text);
            for line in encoded.split("\r\n") {
                assert!(line.len() <= 76, "{}", line.len());
                assert!(line.is_ascii());
            }
            assert_eq!(qp_decode(&encoded), text.as_bytes());
        }
    }

    #[test]
    fn a_three_character_escape_is_never_split() {
        let text = format!("{}è", "a".repeat(73));
        let encoded = quoted_printable(&text);
        let first = encoded.split("\r\n").next().unwrap();
        assert_eq!(first.len(), 74);
        assert!(first.ends_with('='));
        assert_eq!(qp_decode(&encoded), text.as_bytes());
    }

    #[test]
    fn base64_lines_are_76_wide_and_end_with_crlf() {
        let data: Vec<u8> = (0..=255u8).cycle().take(1000).collect();
        let out = base64_lines(&data);
        assert!(out.ends_with("\r\n"));
        let lines: Vec<&str> = out.trim_end().split("\r\n").collect();
        assert!(lines[..lines.len() - 1].iter().all(|line| line.len() == 76));
        assert!(lines.last().unwrap().len() <= 76);
        let joined: String = lines.concat();
        assert_eq!(STANDARD.decode(joined).unwrap(), data);
        assert_eq!(base64_lines(b""), "");
    }

    #[test]
    fn content_types_are_two_tokens() {
        for good in ["text/plain", "application/octet-stream", "image/svg+xml"] {
            assert!(valid_content_type(good), "{good}");
        }
        for bad in [
            "text",
            "/plain",
            "text/",
            "text/pla in",
            "text/plain; charset=x",
            "text/plain\r\nBcc: x",
            "te(xt/plain",
            "a/b/c",
            "tèxt/plain",
        ] {
            assert!(!valid_content_type(bad), "{bad:?}");
        }
        let long = format!("a/{}", "b".repeat(126));
        assert!(!valid_content_type(&long));
        assert!(valid_content_type(&long[..127]));
    }

    #[test]
    fn filenames_get_a_fallback_and_an_rfc2231_form() {
        assert_eq!(filename_params("report.pdf"), ["filename=\"report.pdf\""]);
        assert_eq!(filename_params("a\"b\\c"), ["filename=\"a\\\"b\\\\c\""]);
        assert_eq!(
            filename_params("résumé 1.pdf"),
            [
                "filename=\"r_sum_ 1.pdf\"",
                "filename*=utf-8''r%C3%A9sum%C3%A9%201.pdf"
            ]
        );
    }

    #[test]
    fn rfc2231_keeps_only_attribute_characters() {
        assert_eq!(rfc2231_value("a-b_c.d"), "utf-8''a-b_c.d");
        assert_eq!(rfc2231_value("a b'c*"), "utf-8''a%20b%27c%2A");
    }

    #[test]
    fn the_boundary_has_a_marker_and_the_random_bytes_in_hex() {
        let boundary = boundary(&[0xab; 12]);
        assert_eq!(boundary, "=_hinoki_abababababababababababab");
        assert!(boundary.len() <= 70);
    }
}
