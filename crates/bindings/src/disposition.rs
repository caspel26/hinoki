//! The `content-disposition: attachment` value of a downloaded file.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

/// Why a name cannot be a download name.
const REFUSED: &str = "filename must be a non-empty name without '/' or control characters";

/// Tab is allowed: it is replaced in the ASCII fallback and encoded in the
/// exact name.
fn refused(name: &str) -> bool {
    name.is_empty()
        || name.contains('/')
        || name
            .chars()
            .any(|c| matches!(c, '\u{0}'..='\u{8}' | '\u{a}'..='\u{1f}' | '\u{7f}'))
}

fn percent_encode(name: &str) -> String {
    let mut out = String::with_capacity(name.len());
    for byte in name.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'.' | b'-' | b'~') {
            out.push(char::from(byte));
        } else {
            out.push_str(&format!("%{byte:02X}"));
        }
    }
    out
}

/// `attachment; filename="..."` with an ASCII fallback, plus the exact UTF-8
/// name (`filename*`) when the two differ.
pub(crate) fn attachment(name: &str) -> Result<String, &'static str> {
    if refused(name) {
        return Err(REFUSED);
    }
    let fallback: String = name
        .chars()
        .map(|c| match c {
            ' '..='~' if c != '"' && c != '\\' => c,
            _ => '_',
        })
        .collect();
    let mut value = format!("attachment; filename=\"{fallback}\"");
    if fallback != name {
        value.push_str("; filename*=UTF-8''");
        value.push_str(&percent_encode(name));
    }
    Ok(value)
}

/// `content-disposition` value for a download named `filename`. Raises
/// `ValueError` for an empty name or one with `/` or a control character.
#[pyfunction]
pub(crate) fn content_disposition(filename: &str) -> PyResult<String> {
    attachment(filename).map_err(PyValueError::new_err)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_ascii_name_is_quoted_as_it_is() {
        assert_eq!(
            attachment("a b~c_d-e.f").unwrap(),
            r#"attachment; filename="a b~c_d-e.f""#
        );
        assert_eq!(attachment("x%y").unwrap(), r#"attachment; filename="x%y""#);
    }

    #[test]
    fn other_names_get_a_fallback_and_the_exact_name() {
        assert_eq!(
            attachment("résumé.pdf").unwrap(),
            "attachment; filename=\"r_sum_.pdf\"; filename*=UTF-8''r%C3%A9sum%C3%A9.pdf"
        );
        assert_eq!(
            attachment("a\"b\\c").unwrap(),
            "attachment; filename=\"a_b_c\"; filename*=UTF-8''a%22b%5Cc"
        );
        assert_eq!(
            attachment("😀.png").unwrap(),
            "attachment; filename=\"_.png\"; filename*=UTF-8''%F0%9F%98%80.png"
        );
        assert_eq!(
            attachment("tab\tx").unwrap(),
            "attachment; filename=\"tab_x\"; filename*=UTF-8''tab%09x"
        );
    }

    #[test]
    fn an_empty_name_a_slash_or_a_control_character_is_refused() {
        for name in ["", "a/b", "a\nb", "a\0b", "a\u{7f}b"] {
            assert!(attachment(name).is_err(), "{name:?}");
        }
    }
}
