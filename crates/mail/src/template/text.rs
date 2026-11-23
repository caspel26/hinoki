//! Plain text derived from rendered HTML: one forward scanner, not a parser.
//!
//! Tags are removed or turned into line breaks, links keep their address,
//! entities are decoded and whitespace is collapsed outside `pre`.

use super::error::{Part, Problem, Reason};
use super::limits::MAX_OUTPUT_BYTES;

/// Elements whose whole content is dropped.
const SKIPPED: [&str; 5] = ["head", "style", "script", "title", "template"];
/// Elements whose opening and closing tag separate paragraphs.
const BLOCKS: [&str; 16] = [
    "p",
    "div",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "table",
    "blockquote",
    "section",
    "article",
    "header",
    "footer",
    "ul",
    "ol",
];
const SCHEMES: [&str; 3] = ["http://", "https://", "mailto:"];
const MAX_ENTITY_BYTES: usize = 12;
const MAX_NUMERIC_DIGITS: usize = 7;

fn find(bytes: &[u8], from: usize, needle: &[u8]) -> Option<usize> {
    bytes
        .get(from..)?
        .windows(needle.len())
        .position(|window| window == needle)
        .map(|found| from + found)
}

fn has_prefix_ignoring_case(text: &str, prefix: &str) -> bool {
    text.as_bytes()
        .get(..prefix.len())
        .is_some_and(|head| head.eq_ignore_ascii_case(prefix.as_bytes()))
}

fn numeric(body: &str) -> Option<char> {
    let digits = body.strip_prefix('#')?;
    let (radix, digits) = match digits.strip_prefix(['x', 'X']) {
        Some(hex) => (16, hex),
        None => (10, digits),
    };
    if digits.is_empty()
        || digits.len() > MAX_NUMERIC_DIGITS
        || !digits.chars().all(|ch| ch.is_digit(radix))
    {
        return None;
    }
    char::from_u32(u32::from_str_radix(digits, radix).ok()?)
}

/// The character `rest` starts with, when `rest` begins with a known entity,
/// and the bytes it takes.
fn decode_entity(rest: &str) -> Option<(char, usize)> {
    let end = rest
        .bytes()
        .take(MAX_ENTITY_BYTES)
        .position(|byte| byte == b';')?;
    let ch = match &rest[1..end] {
        "amp" => '&',
        "lt" => '<',
        "gt" => '>',
        "quot" => '"',
        "apos" => '\'',
        "nbsp" => ' ',
        body => numeric(body)?,
    };
    if ch.is_control() && !matches!(ch, '\t' | '\n') {
        return None;
    }
    Some((ch, end + 1))
}

fn decode_entities(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(at) = rest.find('&') {
        out.push_str(&rest[..at]);
        rest = &rest[at..];
        match decode_entity(rest) {
            Some((ch, taken)) => {
                out.push(ch);
                rest = &rest[taken..];
            }
            None => {
                out.push('&');
                rest = &rest[1..];
            }
        }
    }
    out.push_str(rest);
    out
}

fn skip_blank(bytes: &[u8], mut at: usize) -> usize {
    while bytes.get(at).is_some_and(|byte| byte.is_ascii_whitespace()) {
        at += 1;
    }
    at
}

/// The value starting at `at` (after the `=`) and the position after it.
fn attribute_value(attrs: &str, at: usize) -> (&str, usize) {
    let bytes = attrs.as_bytes();
    let (from, until): (usize, fn(u8) -> bool) = match bytes.get(at) {
        Some(b'"') => (at + 1, |byte| byte == b'"'),
        Some(b'\'') => (at + 1, |byte| byte == b'\''),
        _ => (at, |byte| byte.is_ascii_whitespace()),
    };
    let to = bytes[from.min(bytes.len())..]
        .iter()
        .position(|byte| until(*byte))
        .map_or(bytes.len(), |found| from + found);
    let after = if from == at { to } else { to + 1 };
    (&attrs[from.min(to)..to], after)
}

/// The decoded value of attribute `wanted` in the text of a start tag.
fn attribute(attrs: &str, wanted: &str) -> Option<String> {
    let bytes = attrs.as_bytes();
    let mut at = 0;
    loop {
        while bytes
            .get(at)
            .is_some_and(|byte| byte.is_ascii_whitespace() || *byte == b'/')
        {
            at += 1;
        }
        if at >= bytes.len() {
            return None;
        }
        let name_end = bytes[at..]
            .iter()
            .position(|byte| byte.is_ascii_whitespace() || matches!(byte, b'=' | b'/'))
            .map_or(bytes.len(), |found| at + found);
        let name = &attrs[at..name_end];
        at = skip_blank(bytes, name_end);
        let mut value = None;
        if bytes.get(at) == Some(&b'=') {
            let (found, after) = attribute_value(attrs, skip_blank(bytes, at + 1));
            value = Some(found);
            at = after;
        }
        if name.eq_ignore_ascii_case(wanted) {
            return value.map(decode_entities);
        }
    }
}

/// An address worth keeping in text: web or mail, without blanks or controls.
fn safe_href(attrs: &str) -> Option<String> {
    let href = attribute(attrs, "href")?;
    let scheme = SCHEMES
        .iter()
        .any(|scheme| has_prefix_ignoring_case(&href, scheme));
    let clean = !href.chars().any(|ch| ch.is_whitespace() || ch.is_control());
    (scheme && clean).then_some(href)
}

/// Trims lines, allows one blank line in a row and no blank line at the ends.
fn finish(text: &str) -> String {
    let mut lines: Vec<&str> = Vec::new();
    let mut blank = 0;
    for line in text.split('\n') {
        let line = line.trim_end_matches([' ', '\t']);
        blank = if line.is_empty() { blank + 1 } else { 0 };
        if blank <= 1 {
            lines.push(line);
        }
    }
    let Some(first) = lines.iter().position(|line| !line.is_empty()) else {
        return String::new();
    };
    let last = lines
        .iter()
        .rposition(|line| !line.is_empty())
        .unwrap_or(first);
    lines[first..=last].join("\n")
}

struct Link {
    href: Option<String>,
    start: usize,
}

struct Deriver<'a> {
    html: &'a str,
    bytes: &'a [u8],
    out: String,
    /// Whitespace was seen since the last character written.
    space: bool,
    pre: u32,
    link: Option<Link>,
    last_gt: Option<usize>,
    /// Bytes the quote-aware search for the end of a tag may still read; past
    /// it the first `>` ends a tag.
    quote_budget: usize,
}

impl<'a> Deriver<'a> {
    fn new(html: &'a str) -> Self {
        Self {
            html,
            bytes: html.as_bytes(),
            out: String::new(),
            space: false,
            pre: 0,
            link: None,
            last_gt: html.rfind('>'),
            quote_budget: html.len().saturating_mul(4),
        }
    }

    fn push_char(&mut self, ch: char) {
        if self.pre > 0 {
            if ch != '\r' {
                self.out.push(ch);
            }
            return;
        }
        if matches!(ch, ' ' | '\t' | '\r' | '\n') {
            self.space = true;
            return;
        }
        if self.space && !self.out.is_empty() && !self.out.ends_with([' ', '\n']) {
            self.out.push(' ');
        }
        self.space = false;
        self.out.push(ch);
    }

    fn push_text(&mut self, text: &str) {
        for ch in text.chars() {
            self.push_char(ch);
        }
    }

    fn trim_blanks(&mut self) {
        let kept = self.out.trim_end_matches([' ', '\t']).len();
        self.out.truncate(kept);
        if let Some(link) = &mut self.link {
            link.start = link.start.min(kept);
        }
        self.space = false;
    }

    fn block_break(&mut self) {
        self.trim_blanks();
        if self.out.is_empty() {
            return;
        }
        let have = self
            .out
            .bytes()
            .rev()
            .take_while(|byte| *byte == b'\n')
            .count();
        for _ in have.min(2)..2 {
            self.out.push('\n');
        }
    }

    fn line_break(&mut self) {
        self.trim_blanks();
        self.out.push('\n');
    }

    fn end_line(&mut self) {
        self.trim_blanks();
        if !self.out.is_empty() && !self.out.ends_with('\n') {
            self.out.push('\n');
        }
    }

    fn list_item(&mut self) {
        self.end_line();
        self.out.push_str("- ");
    }

    fn open_link(&mut self, attrs: &str) {
        self.close_link();
        self.link = Some(Link {
            href: safe_href(attrs),
            start: self.out.len(),
        });
    }

    fn close_link(&mut self) {
        let Some(Link {
            href: Some(href),
            start,
        }) = self.link.take()
        else {
            return;
        };
        let label = self.out.get(start..).unwrap_or("").trim().to_owned();
        let bare = href.strip_prefix("mailto:").unwrap_or(&href);
        if label == href || label == bare {
            return;
        }
        if label.is_empty() {
            self.push_text(&href);
        } else {
            self.push_text(&format!(" ({href})"));
        }
    }

    fn open_tag(&mut self, name: &str, attrs: &str) {
        match name {
            "br" => self.line_break(),
            "hr" => self.block_break(),
            "li" => self.list_item(),
            "a" => self.open_link(attrs),
            "pre" => {
                self.block_break();
                self.pre += 1;
            }
            _ if BLOCKS.contains(&name) => self.block_break(),
            _ => {}
        }
    }

    fn close_tag(&mut self, name: &str) {
        match name {
            "a" => self.close_link(),
            "tr" => self.end_line(),
            "td" | "th" => self.space = true,
            "pre" => {
                self.pre = self.pre.saturating_sub(1);
                self.block_break();
            }
            _ if BLOCKS.contains(&name) => self.block_break(),
            _ => {}
        }
    }

    /// The `>` that ends a tag whose name ended at `from`: the first one
    /// outside quotes while the budget lasts, else the first one.
    fn tag_end(&mut self, from: usize) -> Option<usize> {
        if self.last_gt.is_none_or(|last| from > last) {
            return None;
        }
        if let Some(end) = self.quoted_end(from) {
            return Some(end);
        }
        find(self.bytes, from, b">")
    }

    fn quoted_end(&mut self, from: usize) -> Option<usize> {
        let bytes = self.bytes;
        let window = &bytes[from..];
        let window = &window[..window.len().min(self.quote_budget)];
        let mut quote: Option<u8> = None;
        let mut found = None;
        for (offset, &byte) in window.iter().enumerate() {
            match (quote, byte) {
                (Some(open), _) if byte == open => quote = None,
                (Some(_), _) => {}
                (None, b'"' | b'\'') => quote = Some(byte),
                (None, b'>') => {
                    found = Some(offset);
                    break;
                }
                _ => {}
            }
        }
        self.quote_budget -= found.map_or(window.len(), |offset| offset + 1);
        found.map(|offset| from + offset)
    }

    /// The position after the close tag of `name` that follows `from`, or the
    /// end of the input.
    fn after_close(&self, name: &str, from: usize) -> usize {
        let mut at = from;
        while let Some(found) = find(self.bytes, at, b"</") {
            let start = found + 2;
            let end = start + name.len();
            let named = self
                .bytes
                .get(start..end)
                .is_some_and(|word| word.eq_ignore_ascii_case(name.as_bytes()));
            let whole = !self.bytes.get(end).is_some_and(u8::is_ascii_alphanumeric);
            if named && whole {
                return find(self.bytes, end, b">").map_or(self.bytes.len(), |gt| gt + 1);
            }
            at = start;
        }
        self.bytes.len()
    }

    fn tag(&mut self, at: usize, closing: bool) -> usize {
        let name_start = at + 1 + usize::from(closing);
        let name_len = self.bytes[name_start..]
            .iter()
            .take_while(|byte| byte.is_ascii_alphanumeric())
            .count();
        let name_end = name_start + name_len;
        let Some(end) = self.tag_end(name_end) else {
            self.push_char('<');
            return at + 1;
        };
        let name = self.html[name_start..name_end].to_ascii_lowercase();
        let after = end + 1;
        if closing {
            self.close_tag(&name);
            return after;
        }
        let html = self.html;
        self.open_tag(&name, &html[name_end..end]);
        if SKIPPED.contains(&name.as_str()) {
            return self.after_close(&name, after);
        }
        after
    }

    fn declaration(&self, at: usize) -> usize {
        let (needle, from) = if self.bytes[at..].starts_with(b"<!--") {
            (&b"-->"[..], at + 4)
        } else {
            (&b">"[..], at + 2)
        };
        find(self.bytes, from, needle).map_or(self.bytes.len(), |found| found + needle.len())
    }

    fn markup(&mut self, at: usize) -> usize {
        let next = self.bytes.get(at + 1).copied();
        match next {
            Some(b'!') => self.declaration(at),
            Some(b'?') => self.declaration_to_gt(at),
            Some(b'/') if self.bytes.get(at + 2).is_some_and(u8::is_ascii_alphabetic) => {
                self.tag(at, true)
            }
            Some(byte) if byte.is_ascii_alphabetic() => self.tag(at, false),
            _ => {
                self.push_char('<');
                at + 1
            }
        }
    }

    fn declaration_to_gt(&self, at: usize) -> usize {
        find(self.bytes, at + 2, b">").map_or(self.bytes.len(), |found| found + 1)
    }

    fn entity(&mut self, at: usize) -> usize {
        match decode_entity(&self.html[at..]) {
            Some((ch, taken)) => {
                self.push_char(ch);
                at + taken
            }
            None => {
                self.push_char('&');
                at + 1
            }
        }
    }

    fn run_of_text(&mut self, at: usize) -> usize {
        let end = self.bytes[at..]
            .iter()
            .position(|byte| matches!(byte, b'<' | b'&'))
            .map_or(self.bytes.len(), |found| at + found);
        let html = self.html;
        self.push_text(&html[at..end]);
        end
    }

    fn run(mut self) -> Result<String, Reason> {
        let mut at = 0;
        while at < self.bytes.len() {
            at = match self.bytes[at] {
                b'<' => self.markup(at),
                b'&' => self.entity(at),
                _ => self.run_of_text(at),
            };
            if self.out.len() > MAX_OUTPUT_BYTES {
                return Err(Reason::OutputTooLarge);
            }
        }
        self.close_link();
        if self.out.len() > MAX_OUTPUT_BYTES {
            return Err(Reason::OutputTooLarge);
        }
        Ok(finish(&self.out))
    }
}

/// The text of `html`, already rendered. Bounded by `MAX_OUTPUT_BYTES`.
pub(crate) fn derive(html: &str) -> Result<String, Problem> {
    Deriver::new(html)
        .run()
        .map_err(|reason| Problem::new(reason).within(Part::Text, None))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn text(html: &str) -> String {
        derive(html).unwrap()
    }

    #[test]
    fn breaks_and_blocks_become_line_breaks_and_paragraphs() {
        assert_eq!(text("a<br>b<br/>c"), "a\nb\nc");
        assert_eq!(text("<p>one</p><p>two</p>"), "one\n\ntwo");
        assert_eq!(text("<h1>T</h1><div>x</div>y"), "T\n\nx\n\ny");
        assert_eq!(text("a<hr>b"), "a\n\nb");
        assert_eq!(text("<ul><li>a</li><li>b</li></ul>"), "- a\n- b");
        assert_eq!(
            text("<table><tr><td>a</td><td>b</td></tr><tr><th>c</th></tr></table>"),
            "a b\nc"
        );
    }

    #[test]
    fn whitespace_collapses_outside_pre_and_stays_inside() {
        assert_eq!(text("  a \n\t b  <b>c</b>\r\n d "), "a b c d");
        assert_eq!(text("<pre>a  b\r\n  c</pre>after"), "a  b\n  c\n\nafter");
        assert_eq!(text("<p>a</p>\n\n\n<p>b</p>"), "a\n\nb");
    }

    #[test]
    fn head_style_script_title_template_and_comments_are_dropped() {
        let html = "<head><title>T</title><style>p{}</style></head>a<script>x < 1</script>\
                    <!-- c -->b<!DOCTYPE html><?php x ?><TEMPLATE>t</TEMPLATE>c";
        assert_eq!(text(html), "abc");
        assert_eq!(text("a<style>never closed"), "a");
        assert_eq!(text("a<!-- open"), "a");
        assert_eq!(text("a<!x"), "a");
        assert_eq!(text("a<?x"), "a");
        assert_eq!(text("a<script>x</scripty>b</script>c"), "ac");
    }

    #[test]
    fn links_keep_their_web_and_mail_addresses() {
        assert_eq!(
            text(r#"<a href="https://example.com/x">Open</a>"#),
            "Open (https://example.com/x)"
        );
        assert_eq!(
            text(r#"<a href='http://example.com'>http://example.com</a>"#),
            "http://example.com"
        );
        assert_eq!(
            text("<a href=mailto:ada@example.com>ada@example.com</a>"),
            "ada@example.com"
        );
        assert_eq!(
            text(r#"<a href="mailto:ada@example.com">Write</a>"#),
            "Write (mailto:ada@example.com)"
        );
        assert_eq!(
            text(r#"<a href="https://example.com/?a=1&amp;b=2">x</a>"#),
            "x (https://example.com/?a=1&b=2)"
        );
        assert_eq!(
            text(r#"<a href="https://example.com"></a>"#),
            "https://example.com"
        );
        assert_eq!(
            text(r#"<A HREF="HTTPS://example.com">x</A>"#),
            "x (HTTPS://example.com)"
        );
    }

    #[test]
    fn links_to_other_schemes_or_with_blanks_keep_only_the_label() {
        for html in [
            r#"<a href="javascript:alert(1)">x</a>"#,
            r#"<a href="/relative">x</a>"#,
            r#"<a href="https://a b">x</a>"#,
            r#"<a href="https://a&#10;b">x</a>"#,
            r#"<a name="n">x</a>"#,
            r#"<a href>x</a>"#,
        ] {
            assert_eq!(text(html), "x", "{html}");
        }
    }

    #[test]
    fn a_nested_or_unclosed_link_closes_the_previous_one() {
        assert_eq!(
            text(r#"<a href="https://a.example.com">A<a href="https://b.example.com">B</a>"#),
            "A (https://a.example.com)B (https://b.example.com)"
        );
        assert_eq!(
            text(r#"<a href="https://a.example.com">A"#),
            "A (https://a.example.com)"
        );
        assert_eq!(text("</a>x"), "x");
    }

    #[test]
    fn attributes_are_read_with_quotes_blanks_and_slashes() {
        assert_eq!(
            text(r#"<a class="c d" data-x='>' href = "https://example.com/p" / >x</a>"#),
            "x (https://example.com/p)"
        );
        assert_eq!(
            text("<a =x href=https://example.com/q>x</a>"),
            "x (https://example.com/q)"
        );
        assert_eq!(
            text(r#"<a href="https://example.com"#),
            "<a href=\"https://example.com"
        );
    }

    #[test]
    fn entities_are_decoded_or_left_alone() {
        assert_eq!(
            text("&amp; &lt; &gt; &quot; &apos; &#39; &#x41; &#X42;"),
            "& < > \" ' ' A B"
        );
        assert_eq!(text("a&nbsp;&nbsp;b"), "a b");
        assert_eq!(
            text("&copy; &#0; &#1; &#xD800; &#x110000; &#+65; &#; &amp"),
            "&copy; &#0; &#1; &#xD800; &#x110000; &#+65; &#; &amp"
        );
        assert_eq!(text("&#9;a&#10;b"), "a b");
        assert_eq!(
            text("&#12345678; &verylongentityname;"),
            "&#12345678; &verylongentityname;"
        );
    }

    #[test]
    fn a_lone_angle_bracket_is_text() {
        assert_eq!(text("1 < 2 and 3 <4"), "1 < 2 and 3 <4");
        assert_eq!(text("a <b and c"), "a <b and c");
        assert_eq!(text("a<"), "a<");
        assert_eq!(text("x</ y"), "x</ y");
    }

    #[test]
    fn unterminated_quotes_fall_back_to_the_first_closing_bracket() {
        assert_eq!(text(r#"<p class="a>b</p>"#), "b");
        assert_eq!(
            text(r#"<a href="https://example.com>x</a>"#),
            "x (https://example.com)"
        );
    }

    #[test]
    fn the_quote_search_budget_runs_out_without_breaking_the_output() {
        let html = "<p class=\"".repeat(200) + ">tail";
        let found = text(&html);
        assert!(found.ends_with("tail"), "{found}");
    }

    #[test]
    fn the_result_has_no_blank_ends_and_no_trailing_blanks() {
        assert_eq!(text("<br><br>a  <br> b<br><br>"), "a\nb");
        assert_eq!(text("   "), "");
        assert_eq!(text("<p></p>"), "");
        assert_eq!(text("a<br><br><br><br>b"), "a\n\nb");
    }

    #[test]
    fn the_output_is_bounded() {
        let long = "x".repeat(MAX_OUTPUT_BYTES + 1);
        let error = derive(&long).unwrap_err();
        assert_eq!(error.reason, Reason::OutputTooLarge);
        assert_eq!(error.part, Some(Part::Text));
        assert!(derive(&"x".repeat(MAX_OUTPUT_BYTES)).is_ok());
    }

    #[test]
    fn a_link_opened_in_pre_keeps_its_label_across_a_break() {
        assert_eq!(
            text("<pre>hello   <a href=\"https://example.com\"><br>aé</a></pre>"),
            "hello\naé (https://example.com)"
        );
        assert_eq!(
            text("<pre>hello   <a href=\"https://example.com\"><br>https://example.com</a></pre>"),
            "hello\nhttps://example.com"
        );
    }

    #[test]
    fn an_unclosed_link_cannot_push_the_output_past_its_bound() {
        let href = "https://example.com/".repeat(100);
        let html = format!("{}<a href=\"{href}\">", "x".repeat(MAX_OUTPUT_BYTES - 10));
        let error = derive(&html).unwrap_err();
        assert_eq!(error.reason, Reason::OutputTooLarge);
    }

    #[test]
    fn pathological_markup_finishes() {
        let html = "<a ".repeat(200_000);
        let _ = text(&html);
        let html = "<a x=\"".repeat(100_000) + ">";
        let _ = text(&html);
        let html = "<style>".repeat(100_000);
        assert_eq!(text(&html), "");
    }
}
