//! Header checks and writing: RFC 2047 encoded words, folding at 78 columns.
//!
//! A header value is turned into *atoms* (pieces that are never split) and
//! [`fold`] joins them, breaking lines between atoms only.

use base64::engine::general_purpose::STANDARD;
use base64::Engine as _;

use crate::address::Mailbox;

/// Most bytes of text in one encoded word: `=?utf-8?b?` + 60 base64 characters
/// + `?=` is 72 characters, under the 75 of RFC 2047.
const WORD_BYTES: usize = 45;
/// Longest unbreakable ASCII token written as is; longer ones are encoded so a
/// line cannot reach the 998 octet limit.
const MAX_PLAIN_TOKEN: usize = 900;
const FOLD_AT: usize = 78;
pub(crate) const MAX_NAME: usize = 76;

/// Header names the renderer writes itself; a caller may not set them.
const RESERVED: [&str; 15] = [
    "from",
    "sender",
    "to",
    "cc",
    "bcc",
    "reply-to",
    "subject",
    "date",
    "message-id",
    "mime-version",
    "content-type",
    "content-transfer-encoding",
    "content-disposition",
    "return-path",
    "received",
];

/// Whether `text` has a control character other than a tab: a line break, NUL
/// or anything else that has no place in a header.
pub(crate) fn has_control_char(text: &str) -> bool {
    text.bytes().any(|b| (b < 0x20 && b != b'\t') || b == 0x7f)
}

pub(crate) fn valid_header_name(name: &str) -> bool {
    (1..=MAX_NAME).contains(&name.len())
        && name.bytes().all(|b| (33..=126).contains(&b) && b != b':')
}

pub(crate) fn is_reserved(name: &str) -> bool {
    RESERVED
        .iter()
        .any(|reserved| name.eq_ignore_ascii_case(reserved))
}

/// RFC 2047 `B` encoded words for `text`, split on character boundaries.
pub(crate) fn encoded_words(text: &str) -> Vec<String> {
    let mut words = Vec::new();
    let mut start = 0;
    while start < text.len() {
        let mut end = (start + WORD_BYTES).min(text.len());
        while !text.is_char_boundary(end) {
            end -= 1;
        }
        words.push(format!(
            "=?utf-8?b?{}?=",
            STANDARD.encode(&text.as_bytes()[start..end])
        ));
        start = end;
    }
    words
}

/// Text that can be written as is: printable ASCII with single inner spaces,
/// and no `=?`, which a reader would take for an encoded word.
fn plain(text: &str) -> bool {
    text.bytes().all(|b| (0x20..=0x7e).contains(&b))
        && !text.contains("=?")
        && !text.starts_with(' ')
        && !text.ends_with(' ')
        && !text.contains("  ")
        && text.split(' ').all(|token| token.len() <= MAX_PLAIN_TOKEN)
}

/// The atoms of an unstructured value such as a subject.
pub(crate) fn unstructured(text: &str) -> Vec<String> {
    if text.is_empty() {
        Vec::new()
    } else if plain(text) {
        text.split(' ').map(str::to_owned).collect()
    } else {
        encoded_words(text)
    }
}

/// Whether a display name can be written as a sequence of RFC 5322 words.
fn bare_words(name: &str) -> bool {
    plain(name)
        && name
            .bytes()
            .all(|b| b == b' ' || b.is_ascii_alphanumeric() || b"!#$%&'*+/=?^_`{|}~-".contains(&b))
}

fn quoted(name: &str) -> String {
    let mut out = String::with_capacity(name.len() + 2);
    out.push('"');
    for ch in name.chars() {
        if matches!(ch, '"' | '\\') {
            out.push('\\');
        }
        out.push(ch);
    }
    out.push('"');
    out
}

/// The atoms of one mailbox: its display name (words, a quoted string or
/// encoded words) and the angle address, followed by `,` when `more` follow.
pub(crate) fn mailbox_atoms(mailbox: &Mailbox, more: bool) -> Vec<String> {
    let address = mailbox.address.as_str();
    let mut atoms = match mailbox.name.as_deref().filter(|name| !name.is_empty()) {
        None => vec![address.to_owned()],
        Some(name) => {
            let mut atoms = if bare_words(name) {
                name.split(' ').map(str::to_owned).collect()
            } else if plain(name) {
                vec![quoted(name)]
            } else {
                encoded_words(name)
            };
            atoms.push(format!("<{address}>"));
            atoms
        }
    };
    if more {
        if let Some(last) = atoms.last_mut() {
            last.push(',');
        }
    }
    atoms
}

/// `Name: atoms...` folded at 78 columns, ending in CRLF.
pub(crate) fn fold(name: &str, atoms: &[String]) -> String {
    let mut out = String::new();
    let mut line = format!("{name}:");
    let mut first = true;
    for atom in atoms {
        if !first && line.len() + 1 + atom.len() > FOLD_AT {
            out.push_str(&line);
            out.push_str("\r\n");
            line = String::from(" ");
            line.push_str(atom);
        } else {
            line.push(' ');
            line.push_str(atom);
        }
        first = false;
    }
    out.push_str(&line);
    out.push_str("\r\n");
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::address::parse_addr_spec;

    fn mailbox(email: &str, name: Option<&str>) -> Mailbox {
        Mailbox {
            address: parse_addr_spec(email).unwrap(),
            name: name.map(str::to_owned),
        }
    }

    fn decode(word: &str) -> String {
        let inner = word
            .strip_prefix("=?utf-8?b?")
            .and_then(|rest| rest.strip_suffix("?="))
            .unwrap();
        String::from_utf8(STANDARD.decode(inner).unwrap()).unwrap()
    }

    #[test]
    fn control_characters_are_detected_but_a_tab_is_not() {
        assert!(has_control_char("a\rb"));
        assert!(has_control_char("a\nb"));
        assert!(has_control_char("a\0b"));
        assert!(has_control_char("a\x07b"));
        assert!(has_control_char("a\x7fb"));
        assert!(!has_control_char("a b\tc ü"));
    }

    #[test]
    fn text_that_looks_like_an_encoded_word_is_encoded() {
        assert_eq!(
            unstructured("=?utf-8?b?QQ==?="),
            encoded_words("=?utf-8?b?QQ==?=")
        );
        assert_eq!(unstructured("a =? b"), encoded_words("a =? b"));
        assert_eq!(unstructured("a = b ?"), vec!["a", "=", "b", "?"]);
        let named = mailbox_atoms(&mailbox("ada@example.com", Some("=?x?q?y?=")), false);
        assert!(named[0].starts_with("=?utf-8?b?"), "{named:?}");
    }

    #[test]
    fn header_names_follow_ftext() {
        assert!(valid_header_name("X-Campaign"));
        assert!(!valid_header_name(""));
        assert!(!valid_header_name("X Campaign"));
        assert!(!valid_header_name("X:Campaign"));
        assert!(!valid_header_name("Ünicode"));
        assert!(valid_header_name(&"a".repeat(76)));
        assert!(!valid_header_name(&"a".repeat(77)));
    }

    #[test]
    fn reserved_names_are_case_insensitive() {
        assert!(is_reserved("Subject"));
        assert!(is_reserved("BCC"));
        assert!(is_reserved("Content-Transfer-Encoding"));
        assert!(!is_reserved("X-Subject"));
    }

    #[test]
    fn encoded_words_stay_short_and_round_trip() {
        let text = "çàè€".repeat(40);
        let words = encoded_words(&text);
        assert!(words.len() > 1);
        assert!(words.iter().all(|word| word.len() <= 75));
        let joined: String = words.iter().map(|word| decode(word)).collect();
        assert_eq!(joined, text);
        assert!(encoded_words("").is_empty());
    }

    #[test]
    fn unstructured_text_is_plain_or_encoded() {
        assert_eq!(unstructured("Hello big world"), ["Hello", "big", "world"]);
        assert!(unstructured("").is_empty());
        for text in [
            "Ciao è qui",
            "two  spaces",
            " lead",
            "trail ",
            "tab\there",
            "bell\u{7}",
        ] {
            let atoms = unstructured(text);
            assert!(
                atoms.iter().all(|atom| atom.starts_with("=?utf-8?b?")),
                "{text:?}"
            );
            let joined: String = atoms.iter().map(|atom| decode(atom)).collect();
            assert_eq!(joined, text);
        }
        let long = "x".repeat(901);
        assert!(unstructured(&long)[0].starts_with("=?utf-8?b?"));
    }

    #[test]
    fn mailboxes_choose_the_right_name_form() {
        let one = mailbox_atoms(&mailbox("ada@example.com", None), false);
        assert_eq!(one, ["ada@example.com"]);
        let bare = mailbox_atoms(&mailbox("ada@example.com", Some("Ada Lovelace")), true);
        assert_eq!(bare, ["Ada", "Lovelace", "<ada@example.com>,"]);
        let special = mailbox_atoms(
            &mailbox("ada@example.com", Some("Lovelace, \"Ada\"")),
            false,
        );
        assert_eq!(special, ["\"Lovelace, \\\"Ada\\\"\"", "<ada@example.com>"]);
        let unicode = mailbox_atoms(&mailbox("ada@example.com", Some("Ada Ünal")), false);
        assert!(unicode[0].starts_with("=?utf-8?b?"));
        assert_eq!(unicode.last().unwrap(), "<ada@example.com>");
        let empty = mailbox_atoms(&mailbox("ada@example.com", Some("")), false);
        assert_eq!(empty, ["ada@example.com"]);
    }

    #[test]
    fn folding_breaks_between_atoms_at_78_columns() {
        let atoms: Vec<String> = (0..30).map(|i| format!("word{i:02}")).collect();
        let out = fold("Subject", &atoms);
        assert!(out.ends_with("\r\n"));
        let lines: Vec<&str> = out.trim_end().split("\r\n").collect();
        assert!(lines.len() > 1);
        assert!(lines.iter().all(|line| line.len() <= 78));
        assert!(lines[1..].iter().all(|line| line.starts_with(' ')));
        let unfolded = out.replace("\r\n ", " ");
        assert_eq!(unfolded.trim_end(), format!("Subject: {}", atoms.join(" ")));
    }

    #[test]
    fn an_atom_longer_than_the_line_is_kept_whole() {
        let atom = "y".repeat(100);
        let out = fold("X", &[atom.clone(), atom.clone()]);
        assert_eq!(out, format!("X: {atom}\r\n {atom}\r\n"));
        assert_eq!(fold("Subject", &[]), "Subject:\r\n");
    }
}
