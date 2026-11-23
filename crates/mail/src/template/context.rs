//! Where in the html each `{{ path }}` sits. Escaping `& < > " '` is enough in
//! text and in a quoted attribute; it is not inside `<script>` or `<style>`,
//! in an unquoted attribute value, in the name of a tag or an attribute or in
//! an event handler, so those are refused when the template is compiled. A
//! quoted address attribute (`href`, `src`, ...) is allowed and its value is
//! checked when the template is rendered.

use super::error::{Part, Problem, Reason};
use super::lexer::Token;

const ADDRESS_ATTRIBUTES: [&str; 6] = [
    "href",
    "src",
    "action",
    "formaction",
    "background",
    "poster",
];

/// The schemes an address attribute may carry; an address without a scheme is
/// relative and always allowed.
const SCHEMES: [&str; 4] = ["http", "https", "mailto", "tel"];

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
enum State {
    #[default]
    Data,
    Open,
    Name,
    InTag,
    Attr,
    AfterAttr,
    AfterEq,
    Quoted(char),
    Unquoted,
    Raw,
    Skip,
}

fn is_blank(ch: char) -> bool {
    matches!(ch, ' ' | '\t' | '\r' | '\n' | '\x0c')
}

#[derive(Default)]
struct Scanner {
    state: Option<State>,
    tag: String,
    attr: String,
    closing: String,
    tail: String,
}

impl Scanner {
    fn start_name(&mut self, ch: char) {
        self.tag.clear();
        self.tag.push(ch.to_ascii_lowercase());
        self.state = Some(State::Name);
    }

    fn start_attr(&mut self, ch: char) {
        self.attr.clear();
        self.attr.push(ch.to_ascii_lowercase());
        self.state = Some(State::Attr);
    }

    fn end_tag(&mut self) {
        if matches!(self.tag.as_str(), "script" | "style") {
            self.closing = format!("</{}", self.tag);
            self.tail.clear();
            self.state = Some(State::Raw);
        } else {
            self.state = Some(State::Data);
        }
    }

    fn raw(&mut self, ch: char) {
        self.tail.push(ch.to_ascii_lowercase());
        while self.tail.len() > self.closing.len() {
            self.tail.remove(0);
        }
        if self.tail == self.closing {
            self.tag.clear();
            self.state = Some(State::InTag);
        }
    }

    fn name(&mut self, ch: char) {
        match ch {
            '>' => self.end_tag(),
            '/' => self.state = Some(State::InTag),
            ch if is_blank(ch) => self.state = Some(State::InTag),
            ch => self.tag.push(ch.to_ascii_lowercase()),
        }
    }

    fn in_tag(&mut self, ch: char) {
        match ch {
            '>' => self.end_tag(),
            '/' => {}
            ch if is_blank(ch) => {}
            ch => self.start_attr(ch),
        }
    }

    fn attr(&mut self, ch: char) {
        match ch {
            '>' => self.end_tag(),
            '=' => self.state = Some(State::AfterEq),
            '/' => self.state = Some(State::InTag),
            ch if is_blank(ch) => self.state = Some(State::AfterAttr),
            ch => self.attr.push(ch.to_ascii_lowercase()),
        }
    }

    fn after_attr(&mut self, ch: char) {
        match ch {
            '>' => self.end_tag(),
            '=' => self.state = Some(State::AfterEq),
            ch if is_blank(ch) => {}
            ch => self.start_attr(ch),
        }
    }

    fn after_eq(&mut self, ch: char) {
        match ch {
            '>' => self.end_tag(),
            '"' | '\'' => self.state = Some(State::Quoted(ch)),
            ch if is_blank(ch) => {}
            _ => self.state = Some(State::Unquoted),
        }
    }

    fn open(&mut self, ch: char) {
        match ch {
            ch if ch.is_ascii_alphabetic() => self.start_name(ch),
            '!' | '?' | '/' => self.state = Some(State::Skip),
            '<' => {}
            _ => self.state = Some(State::Data),
        }
    }

    fn step(&mut self, ch: char) {
        match self.state.unwrap_or_default() {
            State::Data if ch == '<' => self.state = Some(State::Open),
            State::Data => {}
            State::Open => self.open(ch),
            State::Name => self.name(ch),
            State::InTag => self.in_tag(ch),
            State::Attr => self.attr(ch),
            State::AfterAttr => self.after_attr(ch),
            State::AfterEq => self.after_eq(ch),
            State::Quoted(quote) if ch == quote => self.state = Some(State::InTag),
            State::Quoted(_) => {}
            State::Unquoted if ch == '>' => self.end_tag(),
            State::Unquoted if is_blank(ch) => self.state = Some(State::InTag),
            State::Unquoted => {}
            State::Raw => self.raw(ch),
            State::Skip if ch == '>' => self.state = Some(State::Data),
            State::Skip => {}
        }
    }

    fn feed(&mut self, text: &str) {
        for ch in text.chars() {
            self.step(ch);
        }
    }

    /// Whether an output here is an address (true) or plain escaped text
    /// (false); `None` when escaping cannot make it safe.
    fn slot(&self) -> Option<bool> {
        match self.state.unwrap_or_default() {
            State::Data => Some(false),
            State::Quoted(_) if self.attr.starts_with("on") => None,
            State::Quoted(_) => Some(ADDRESS_ATTRIBUTES.contains(&self.attr.as_str())),
            _ => None,
        }
    }
}

/// Marks the outputs that sit in an address attribute and refuses those that
/// sit where escaping is not enough.
pub(crate) fn classify(tokens: Vec<Token>) -> Result<Vec<Token>, Problem> {
    let mut scanner = Scanner::default();
    let mut found = Vec::with_capacity(tokens.len());
    for token in tokens {
        match token {
            Token::Text(text) => {
                scanner.feed(&text);
                found.push(Token::Text(text));
            }
            Token::Output { path, at, .. } => {
                let Some(url) = scanner.slot() else {
                    let problem = Problem::new(Reason::UnsafeContext).within(Part::Html, Some(at));
                    return Err(problem.named(path.text()));
                };
                found.push(Token::Output { path, at, url });
            }
            other => found.push(other),
        }
    }
    Ok(found)
}

/// Whether `value` may be the value of an address attribute: no scheme, or one
/// of `SCHEMES`.
pub(crate) fn safe_address(value: &str) -> bool {
    // A browser drops tabs and line breaks inside a scheme, so none is allowed.
    if value.chars().any(char::is_control) {
        return false;
    }
    let value = value.trim_start();
    let Some(colon) = value.find(':') else {
        return true;
    };
    let scheme = &value[..colon];
    if scheme.contains(['/', '?', '#']) {
        return true;
    }
    SCHEMES
        .iter()
        .any(|allowed| scheme.eq_ignore_ascii_case(allowed))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::template::lexer::tokens;

    fn outcome(src: &str) -> Result<Vec<bool>, Reason> {
        let found = classify(tokens(src, Part::Html).unwrap()).map_err(|p| p.reason)?;
        Ok(found
            .iter()
            .filter_map(|token| match token {
                Token::Output { url, .. } => Some(*url),
                _ => None,
            })
            .collect())
    }

    #[test]
    fn text_and_quoted_attributes_are_plain_slots() {
        assert_eq!(
            outcome("<p class=\"{{ a }}\">{{ b }}</p>"),
            Ok(vec![false, false])
        );
        assert_eq!(
            outcome("<a title='x {{ a }}'>{{ b }}</a>"),
            Ok(vec![false, false])
        );
        assert_eq!(
            outcome("a < {{ b }} <!-- c --> {{ d }}"),
            Ok(vec![false, false])
        );
    }

    #[test]
    fn quoted_address_attributes_are_marked() {
        assert_eq!(
            outcome("<a href=\"{{ u }}\">{{ t }}</a>"),
            Ok(vec![true, false])
        );
        assert_eq!(
            outcome("<IMG SRC = '{{ u }}' alt=\"{{ a }}\">"),
            Ok(vec![true, false])
        );
        assert_eq!(outcome("<a href=\"/x/{{ u }}\">"), Ok(vec![true]));
    }

    #[test]
    fn what_escaping_cannot_make_safe_is_refused() {
        for src in [
            "<a href={{ u }}>",
            "<a title=x{{ u }}>",
            "<a {{ u }}>",
            "<{{ u }}>",
            "<a onclick=\"{{ u }}\">",
            "<script>var a = '{{ u }}';</script>",
            "<style>p { color: {{ u }} }</style>",
            "</a {{ u }}>",
        ] {
            assert_eq!(outcome(src), Err(Reason::UnsafeContext), "{src}");
        }
    }

    #[test]
    fn a_script_ends_at_its_closing_tag() {
        assert_eq!(
            outcome("<script>a</SCRIPT ><p>{{ x }}</p>"),
            Ok(vec![false])
        );
    }

    #[test]
    fn addresses_without_a_scheme_or_with_a_safe_one_pass() {
        for ok in [
            "",
            "/a",
            "a/b:c",
            "?q=a:b",
            "#x",
            "https://a",
            "HTTP://a",
            "mailto:a@b",
            "tel:1",
        ] {
            assert!(safe_address(ok), "{ok}");
        }
        for bad in [
            "javascript:alert(1)",
            " JavaScript:x",
            "\tjava:x",
            "data:text/html,x",
            "vbscript:x",
            "file:///etc",
        ] {
            assert!(!safe_address(bad), "{bad}");
        }
    }
}
