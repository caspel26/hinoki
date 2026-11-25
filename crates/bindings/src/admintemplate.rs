//! Pre-compiled HTML template engine and HTML escaper for the Hinoki admin UI.

use pyo3::exceptions::{PyKeyError, PyTypeError};
use pyo3::prelude::*;
use pyo3::types::PyDict;

fn write_escaped(out: &mut String, text: &str) {
    for ch in text.chars() {
        match ch {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#x27;"),
            _ => out.push(ch),
        }
    }
}

#[pyfunction]
pub fn html_escape(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    write_escaped(&mut out, text);
    out
}

#[derive(Clone, Debug, PartialEq, Eq)]
enum Segment {
    Literal(String),
    Escaped(String),
    Raw(String),
}

fn is_word_char(ch: char) -> bool {
    ch.is_alphanumeric() || ch == '_'
}

/// Try to match `{{{ \s* \w+ \s* }}}` (if `triple`) or `{{ \s* \w+ \s* }}` at the start of `s`.
/// Returns `(name, consumed_bytes)`.
fn match_placeholder(s: &str, triple: bool) -> Option<(&str, usize)> {
    let open = if triple { "{{{" } else { "{{" };
    let close = if triple { "}}}" } else { "}}" };
    let after_open = s.strip_prefix(open)?;
    let trimmed = after_open.trim_start();
    let name_len = trimmed
        .char_indices()
        .take_while(|&(_, ch)| is_word_char(ch))
        .last()
        .map(|(idx, ch)| idx + ch.len_utf8())?;
    let name = &trimmed[..name_len];
    let after_name = trimmed[name_len..].trim_start();
    let rest = after_name.strip_prefix(close)?;
    let consumed = s.len() - rest.len();
    Some((name, consumed))
}

fn compile_segments(source: &str) -> Vec<Segment> {
    let mut out = Vec::new();
    let mut lit_start = 0;
    let mut pos = 0;
    while pos < source.len() {
        let Some(rel) = source[pos..].find("{{") else {
            break;
        };
        let cand = pos + rel;
        let tail = &source[cand..];
        let matched = match_placeholder(tail, true)
            .map(|(n, len)| (n, len, true))
            .or_else(|| match_placeholder(tail, false).map(|(n, len)| (n, len, false)));
        if let Some((name, len, is_raw)) = matched {
            if cand > lit_start {
                out.push(Segment::Literal(source[lit_start..cand].to_owned()));
            }
            if is_raw {
                out.push(Segment::Raw(name.to_owned()));
            } else {
                out.push(Segment::Escaped(name.to_owned()));
            }
            pos = cand + len;
            lit_start = pos;
        } else {
            pos = cand + 1;
        }
    }
    if lit_start < source.len() {
        out.push(Segment::Literal(source[lit_start..].to_owned()));
    }
    out
}

#[pyfunction]
pub fn template_placeholders(source: &str) -> Vec<(String, bool)> {
    let mut found = Vec::new();
    for seg in compile_segments(source) {
        match seg {
            Segment::Escaped(name) => found.push((name, false)),
            Segment::Raw(name) => found.push((name, true)),
            Segment::Literal(_) => {}
        }
    }
    found
}

#[pyclass(name = "AdminTemplate", module = "hinoki.core")]
pub struct AdminTemplate {
    segments: Vec<Segment>,
    base_len: usize,
}

#[pymethods]
impl AdminTemplate {
    #[new]
    fn new(source: &str) -> Self {
        let segments = compile_segments(source);
        let base_len = source.len();
        Self { segments, base_len }
    }

    fn render(&self, values: &Bound<'_, PyDict>, raw_type: &Bound<'_, PyAny>) -> PyResult<String> {
        let mut out = String::with_capacity(self.base_len + 64);
        for seg in &self.segments {
            match seg {
                Segment::Literal(lit) => out.push_str(lit),
                Segment::Raw(key) => {
                    let Some(val) = values.get_item(key)? else {
                        return Err(PyKeyError::new_err(key.clone()));
                    };
                    if !val.is_instance(raw_type)? {
                        return Err(PyTypeError::new_err(key.clone()));
                    }
                    let text: String = val.extract()?;
                    out.push_str(&text);
                }
                Segment::Escaped(key) => {
                    let Some(val) = values.get_item(key)? else {
                        return Err(PyKeyError::new_err(key.clone()));
                    };
                    let text = val.str()?;
                    write_escaped(&mut out, &text.to_cow()?);
                }
            }
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn escapes_html_and_parses_placeholders() {
        assert_eq!(
            html_escape(r#"<a href="x&y">'z'</a>"#),
            "&lt;a href=&quot;x&amp;y&quot;&gt;&#x27;z&#x27;&lt;/a&gt;"
        );
        let segs = compile_segments("<p>{{ title }}</p>{{{ body }}}");
        assert_eq!(
            segs,
            vec![
                Segment::Literal("<p>".to_owned()),
                Segment::Escaped("title".to_owned()),
                Segment::Literal("</p>".to_owned()),
                Segment::Raw("body".to_owned()),
            ]
        );
        assert_eq!(
            template_placeholders("{{ a }} {{{ b }}} {{ not a key }}"),
            vec![("a".to_owned(), false), ("b".to_owned(), true)]
        );
    }
}
