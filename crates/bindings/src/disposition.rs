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

/// Media types a browser renders inline without running script in the
/// application's origin. Anything else (HTML, SVG, XML, JavaScript, ...) is
/// sent as an attachment even when `inline` was asked for.
const INLINE_SAFE: [&str; 7] = [
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/avif",
    "application/pdf",
    "text/plain",
];

/// Whether `content_type` (parameters ignored) may be displayed inline.
pub(crate) fn inline_safe(content_type: &str) -> bool {
    let essence = content_type.split(';').next().unwrap_or_default().trim();
    INLINE_SAFE
        .iter()
        .any(|safe| safe.eq_ignore_ascii_case(essence))
}

/// The `content-disposition` of a stored file: `inline` only for a safe media
/// type, `attachment` otherwise, with the download name when there is one.
pub(crate) fn stored(
    kind: &str,
    filename: &str,
    content_type: &str,
) -> Result<String, &'static str> {
    let kind = match kind {
        "inline" if inline_safe(content_type) => "inline",
        "inline" | "attachment" => "attachment",
        _ => return Err("disposition must be 'attachment' or 'inline'"),
    };
    if filename.is_empty() {
        return Ok(kind.to_owned());
    }
    let value = attachment(filename)?;
    Ok(format!("{kind}{}", &value["attachment".len()..]))
}

/// `content-disposition` value for a stored file served with `content_type`.
/// `inline` is kept only for an allowlisted media type that cannot run script
/// in the page's origin; any other type is downgraded to `attachment`. An
/// empty `filename` sends no name. Raises `ValueError` for an unknown
/// `disposition` or a name `content_disposition` refuses.
#[pyfunction]
pub(crate) fn storage_disposition(
    disposition: &str,
    filename: &str,
    content_type: &str,
) -> PyResult<String> {
    stored(disposition, filename, content_type).map_err(PyValueError::new_err)
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

    #[test]
    fn only_passive_media_types_stay_inline() {
        for safe in [
            "image/png",
            "IMAGE/JPEG",
            "text/plain; charset=utf-8",
            "application/pdf",
        ] {
            assert_eq!(stored("inline", "", safe).unwrap(), "inline", "{safe}");
        }
        for active in [
            "text/html",
            "image/svg+xml",
            "application/xhtml+xml",
            "text/javascript",
            "application/xml",
            "application/octet-stream",
            "",
        ] {
            assert_eq!(
                stored("inline", "", active).unwrap(),
                "attachment",
                "{active}"
            );
        }
    }

    #[test]
    fn a_stored_disposition_carries_the_download_name() {
        assert_eq!(
            stored("inline", "a.png", "image/png").unwrap(),
            r#"inline; filename="a.png""#
        );
        assert_eq!(
            stored("inline", "x.html", "text/html").unwrap(),
            r#"attachment; filename="x.html""#
        );
        assert_eq!(
            stored("attachment", "é.txt", "text/plain").unwrap(),
            "attachment; filename=\"_.txt\"; filename*=UTF-8''%C3%A9.txt"
        );
        assert_eq!(
            stored("attachment", "", "text/plain").unwrap(),
            "attachment"
        );
    }

    #[test]
    fn an_unknown_disposition_or_a_bad_name_is_refused() {
        assert!(stored("form-data", "", "text/plain").is_err());
        assert!(stored("inline", "a/b", "image/png").is_err());
    }
}
