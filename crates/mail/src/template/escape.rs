//! HTML escaping of inserted values, and the characters a subject refuses.

/// Whether `ch` cannot appear in a subject: a control character other than a
/// tab (C0, DEL and C1, so NEL too) or a Unicode line or paragraph separator.
pub(crate) fn refused_in_subject(ch: char) -> bool {
    (ch.is_control() && ch != '\t') || matches!(ch, '\u{2028}' | '\u{2029}')
}

/// Appends `text` with `& < > " '` replaced by character references.
pub(crate) fn escape_html_into(out: &mut String, text: &str) {
    for ch in text.chars() {
        match ch {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#x27;"),
            other => out.push(other),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn escaped(text: &str) -> String {
        let mut out = String::new();
        escape_html_into(&mut out, text);
        out
    }

    #[test]
    fn the_five_characters_are_replaced() {
        assert_eq!(
            escaped(r#"<a href="x">Tom & 'Jerry'</a>"#),
            "&lt;a href=&quot;x&quot;&gt;Tom &amp; &#x27;Jerry&#x27;&lt;/a&gt;"
        );
    }

    #[test]
    fn a_subject_refuses_every_line_break_and_control_but_the_tab() {
        for bad in ['\r', '\n', '\0', '\x7f', '\u{85}', '\u{2028}', '\u{2029}'] {
            assert!(refused_in_subject(bad), "{bad:?}");
        }
        for fine in ['\t', ' ', 'é', '☃'] {
            assert!(!refused_in_subject(fine), "{fine:?}");
        }
    }

    #[test]
    fn everything_else_passes_through() {
        assert_eq!(escaped("héllo ☃ 日本\n\t"), "héllo ☃ 日本\n\t");
        assert_eq!(escaped("&amp;"), "&amp;amp;");
    }
}
