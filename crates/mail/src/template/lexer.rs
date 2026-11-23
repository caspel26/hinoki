//! Splits template source into text, `{{ path }}` outputs and `{% tag %}`
//! blocks, with the position of each tag.

use super::error::{Part, Pos, Problem, Reason};
use super::escape::refused_in_subject;
use super::limits::{MAX_IDENT_BYTES, MAX_PATH_SEGMENTS};

/// A dotted variable path of 1 to `MAX_PATH_SEGMENTS` identifiers.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Path {
    pub segments: Vec<String>,
}

impl Path {
    pub fn text(&self) -> String {
        self.segments.join(".")
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum Tag {
    If(Path),
    Else,
    EndIf,
    For { var: String, iter: Path },
    EndFor,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum Token {
    Text(String),
    Output { path: Path, at: Pos, url: bool },
    Tag { tag: Tag, at: Pos },
}

const KEYWORDS: [&str; 6] = ["if", "else", "endif", "for", "in", "endfor"];

fn is_blank(byte: u8) -> bool {
    byte == b' ' || byte == b'\t'
}

fn is_ident(text: &str) -> bool {
    let mut bytes = text.bytes();
    let first = bytes
        .next()
        .is_some_and(|b| b.is_ascii_alphabetic() || b == b'_');
    first && bytes.all(|b| b.is_ascii_alphanumeric() || b == b'_')
}

pub(crate) fn parse_path(text: &str) -> Result<Path, Reason> {
    let segments: Vec<&str> = text.split('.').collect();
    if !segments.iter().all(|segment| is_ident(segment)) {
        return Err(Reason::Syntax);
    }
    if segments.len() > MAX_PATH_SEGMENTS || segments.iter().any(|s| s.len() > MAX_IDENT_BYTES) {
        return Err(Reason::InvalidPath);
    }
    Ok(Path {
        segments: segments.into_iter().map(str::to_owned).collect(),
    })
}

fn parse_var(text: &str) -> Result<String, Reason> {
    if !is_ident(text) || KEYWORDS.contains(&text) {
        return Err(Reason::Syntax);
    }
    if text.len() > MAX_IDENT_BYTES {
        return Err(Reason::InvalidPath);
    }
    Ok(text.to_owned())
}

fn parse_tag(inner: &str) -> Result<Tag, Reason> {
    let words: Vec<&str> = inner
        .split([' ', '\t'])
        .filter(|word| !word.is_empty())
        .collect();
    match words.as_slice() {
        ["if", path] => Ok(Tag::If(parse_path(path)?)),
        ["else"] => Ok(Tag::Else),
        ["endif"] => Ok(Tag::EndIf),
        ["endfor"] => Ok(Tag::EndFor),
        ["for", var, "in", iter] => Ok(Tag::For {
            var: parse_var(var)?,
            iter: parse_path(iter)?,
        }),
        _ => Err(Reason::Syntax),
    }
}

/// The 1-based line and column (in characters) of byte `offset`.
pub(crate) fn position(src: &str, offset: usize) -> Pos {
    Cursor::new(src).pos(offset)
}

/// Walks forward through the source, so positions of increasing offsets cost
/// one pass in total.
struct Cursor<'a> {
    src: &'a str,
    offset: usize,
    line: u32,
    column: u32,
}

impl<'a> Cursor<'a> {
    fn new(src: &'a str) -> Self {
        Self {
            src,
            offset: 0,
            line: 1,
            column: 1,
        }
    }

    fn pos(&mut self, to: usize) -> Pos {
        let to = to.min(self.src.len());
        for ch in self.src[self.offset..to].chars() {
            if ch == '\n' {
                self.line = self.line.saturating_add(1);
                self.column = 1;
            } else {
                self.column = self.column.saturating_add(1);
            }
        }
        self.offset = to;
        Pos {
            line: self.line,
            column: self.column,
        }
    }
}

fn check_controls(src: &str, part: Part) -> Result<(), Problem> {
    let found = if part == Part::Subject {
        src.char_indices().find(|(_, ch)| refused_in_subject(*ch))
    } else {
        src.char_indices().find(|(_, ch)| *ch == '\0')
    };
    match found {
        Some((offset, _)) => {
            Err(Problem::new(Reason::ControlCharacter).within(part, Some(position(src, offset))))
        }
        None => Ok(()),
    }
}

/// The next `{{` (true) or `{%` (false) at or after `from`.
fn find_open(src: &str, from: usize) -> Option<(usize, bool)> {
    let bytes = src.as_bytes();
    let mut at = from;
    while let Some(found) = bytes[at..].iter().position(|&b| b == b'{') {
        let open = at + found;
        match bytes.get(open + 1) {
            Some(b'{') => return Some((open, true)),
            Some(b'%') => return Some((open, false)),
            _ => at = open + 1,
        }
    }
    None
}

struct Lexer<'a> {
    src: &'a str,
    part: Part,
    cursor: Cursor<'a>,
    out: Vec<Token>,
    text_from: usize,
}

impl Lexer<'_> {
    fn problem(&self, reason: Reason, offset: usize) -> Problem {
        Problem::new(reason).within(self.part, Some(position(self.src, offset)))
    }

    fn push_text(&mut self, end: usize) {
        let from = self.text_from.min(end);
        if from < end {
            self.out.push(Token::Text(self.src[from..end].to_owned()));
        }
    }

    /// When the tag spans a whole line, the end of the text before it and the
    /// start of the text after it, so that the line disappears.
    fn standalone(&self, open: usize, end: usize) -> Option<(usize, usize)> {
        let bytes = self.src.as_bytes();
        let mut start = open;
        while start > self.text_from && is_blank(bytes[start - 1]) {
            start -= 1;
        }
        if start != 0 && bytes[start - 1] != b'\n' {
            return None;
        }
        let mut after = end;
        while after < bytes.len() && is_blank(bytes[after]) {
            after += 1;
        }
        let resume = match bytes.get(after) {
            None => after,
            Some(b'\n') => after + 1,
            Some(b'\r') if bytes.get(after + 1) == Some(&b'\n') => after + 2,
            _ => return None,
        };
        Some((start, resume))
    }

    /// The tag content between `inner_start` and `close`, trimmed, as a byte
    /// range, and the position of its first character.
    fn inner(&mut self, inner_start: usize, close: usize) -> (std::ops::Range<usize>, Pos) {
        let raw = &self.src[inner_start..close];
        let lead = raw.len() - raw.trim_start_matches([' ', '\t']).len();
        let trail = raw.len() - raw.trim_end_matches([' ', '\t']).len();
        let pos = self.cursor.pos(inner_start + lead);
        let start = inner_start + lead;
        (start..close.saturating_sub(trail).max(start), pos)
    }

    fn step(&mut self, open: usize, output: bool) -> Result<(), Problem> {
        let closer = if output { "}}" } else { "%}" };
        let inner_start = open + 2;
        let Some(found) = self.src[inner_start..].find(closer) else {
            return Err(self.problem(Reason::UnclosedTag, open));
        };
        let close = inner_start + found;
        let end = close + 2;
        let part = self.part;
        let (range, at) = self.inner(inner_start, close);
        let inner = &self.src[range];
        let fail = |reason| Problem::new(reason).within(part, Some(at));
        if output {
            let path = parse_path(inner).map_err(fail)?;
            self.push_text(open);
            self.out.push(Token::Output {
                path,
                at,
                url: false,
            });
            self.text_from = end;
            return Ok(());
        }
        let tag = parse_tag(inner).map_err(fail)?;
        let (text_end, resume) = self.standalone(open, end).unwrap_or((open, end));
        self.push_text(text_end);
        self.out.push(Token::Tag { tag, at });
        self.text_from = resume;
        Ok(())
    }

    fn run(mut self) -> Result<Vec<Token>, Problem> {
        while let Some((open, output)) = find_open(self.src, self.text_from) {
            self.step(open, output)?;
        }
        self.push_text(self.src.len());
        Ok(self.out)
    }
}

/// The tokens of `src`, a template part.
pub(crate) fn tokens(src: &str, part: Part) -> Result<Vec<Token>, Problem> {
    check_controls(src, part)?;
    Lexer {
        src,
        part,
        cursor: Cursor::new(src),
        out: Vec::new(),
        text_from: 0,
    }
    .run()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lex(src: &str) -> Result<Vec<Token>, Problem> {
        tokens(src, Part::Html)
    }

    fn path(text: &str) -> Path {
        parse_path(text).unwrap()
    }

    fn reason(src: &str) -> Reason {
        lex(src).unwrap_err().reason
    }

    #[test]
    fn splits_text_outputs_and_tags_with_positions() {
        let found = lex("héé {{ a.b }}\n{% if x %}y").unwrap();
        assert_eq!(
            found,
            vec![
                Token::Text("héé ".to_owned()),
                Token::Output {
                    path: path("a.b"),
                    at: Pos { line: 1, column: 8 },
                    url: false
                },
                Token::Text("\n".to_owned()),
                Token::Tag {
                    tag: Tag::If(path("x")),
                    at: Pos { line: 2, column: 4 }
                },
                Token::Text("y".to_owned()),
            ]
        );
    }

    #[test]
    fn a_lone_brace_or_percent_is_text_so_css_works() {
        let found = lex("a { color: red } 100% {x}").unwrap();
        assert_eq!(found, vec![Token::Text("a { color: red } 100% {x}".into())]);
    }

    #[test]
    fn tag_whitespace_is_optional_and_tabs_count() {
        let found = lex("{{x}}{%\tif\tx%}{%for a in b%}").unwrap();
        assert_eq!(found.len(), 3);
        assert!(matches!(
            &found[2],
            Token::Tag { tag: Tag::For { var, iter }, .. } if var == "a" && iter.text() == "b"
        ));
    }

    #[test]
    fn standalone_tag_lines_vanish_with_their_indent_and_line_ending() {
        let found = lex("a\n  {% if x %}  \nb\r\n\t{% endif %}\r\nc").unwrap();
        let texts: Vec<_> = found
            .iter()
            .filter_map(|t| match t {
                Token::Text(text) => Some(text.as_str()),
                _ => None,
            })
            .collect();
        assert_eq!(texts, ["a\n", "b\r\n", "c"]);
        let eof = lex("a\n{% endif %}").unwrap();
        assert_eq!(eof.len(), 2);
    }

    #[test]
    fn a_tag_sharing_its_line_keeps_the_text_around_it() {
        let found = lex("x {% if a %}\n").unwrap();
        assert_eq!(found[0], Token::Text("x ".into()));
        assert_eq!(found[2], Token::Text("\n".into()));
        let found = lex("{{ a }} {% if a %}\n").unwrap();
        assert_eq!(found[1], Token::Text(" ".into()));
        let found = lex("{% if a %} x\n").unwrap();
        assert_eq!(found[1], Token::Text(" x\n".into()));
        let found = lex("{% if a %}\r").unwrap();
        assert_eq!(found[1], Token::Text("\r".into()));
    }

    #[test]
    fn output_lines_are_never_removed() {
        let found = lex("{{ a }}\n").unwrap();
        assert_eq!(found[1], Token::Text("\n".into()));
    }

    #[test]
    fn unclosed_tags_are_refused_at_their_start() {
        let error = lex("ab\n{{ x").unwrap_err();
        assert_eq!(error.reason, Reason::UnclosedTag);
        assert_eq!(error.at, Some(Pos { line: 2, column: 1 }));
        assert_eq!(reason("{% if x }}"), Reason::UnclosedTag);
    }

    #[test]
    fn anything_but_a_path_in_an_output_is_a_syntax_error() {
        for src in [
            "{{ a | b }}",
            "{{ 'x' }}",
            "{{ }}",
            "{# c #}{{ a b }}",
            "{{ a. }}",
            "{{ .a }}",
            "{{ 1a }}",
            "{{ a() }}",
            "{{ a . b }}",
            "{{ {{ a }}",
        ] {
            assert_eq!(reason(src), Reason::Syntax, "{src}");
        }
    }

    #[test]
    fn tags_other_than_the_known_forms_are_syntax_errors() {
        for src in [
            "{% elif x %}",
            "{% if %}",
            "{% if not x %}",
            "{% else x %}",
            "{% for x %}",
            "{% for x of y %}",
            "{% for if in y %}",
            "{% for a.b in y %}",
            "{% include a %}",
            "{% %}",
            "{%if%}",
        ] {
            assert_eq!(reason(src), Reason::Syntax, "{src}");
        }
    }

    #[test]
    fn paths_and_names_have_length_limits() {
        let ident = "a".repeat(MAX_IDENT_BYTES);
        assert!(lex(&format!("{{{{ {ident} }}}}")).is_ok());
        assert_eq!(reason(&format!("{{{{ {ident}a }}}}")), Reason::InvalidPath);
        let eight = ["a"; MAX_PATH_SEGMENTS].join(".");
        assert!(lex(&format!("{{{{ {eight} }}}}")).is_ok());
        assert_eq!(reason(&format!("{{{{ {eight}.a }}}}")), Reason::InvalidPath);
        assert_eq!(
            reason(&format!("{{% for {ident}a in b %}}")),
            Reason::InvalidPath
        );
    }

    #[test]
    fn a_subject_refuses_control_characters_in_its_source() {
        let error = tokens("a\nb", Part::Subject).unwrap_err();
        assert_eq!(error.reason, Reason::ControlCharacter);
        assert_eq!(error.part, Some(Part::Subject));
        assert!(tokens("a\tb", Part::Subject).is_ok());
        assert!(tokens("a\x7fb", Part::Subject).is_err());
        assert!(tokens("a\nb", Part::Text).is_ok());
        assert_eq!(
            tokens("a\0b", Part::Html).unwrap_err().reason,
            Reason::ControlCharacter
        );
    }
}
