//! The email template engine: a small fixed language (`{{ path }}`,
//! `{% if %}`, `{% for %}`) compiled once, verified against the shape of its
//! context, and rendered to a subject, a text part and an html part.
//!
//! Values inserted in the html part are escaped, values inserted in the
//! subject cannot contain a line break, and every size is bounded.

mod context;
mod error;
mod escape;
mod lexer;
pub mod limits;
mod parser;
mod render;
mod shape;
mod text;
mod value;

#[cfg(test)]
mod tests;

pub use error::{Part, Pos, Problem, Reason, TemplateError};
pub use shape::{Refusal, Shape};
pub use value::{Map, Value};

use lexer::tokens;
use limits::{MAX_BODY_SOURCE_BYTES, MAX_NAME_BYTES, MAX_SUBJECT_SOURCE_BYTES};
use parser::{parse, Budget, Node};

/// The rendered parts of a template.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Rendered {
    pub subject: String,
    pub text: String,
    pub html: String,
}

/// A compiled template: `compile` checks the syntax and the sizes, `verify`
/// checks the variables against a context shape, `render` produces the parts.
#[derive(Clone, Debug)]
pub struct Template {
    name: String,
    subject: Vec<Node>,
    text: Option<Vec<Node>>,
    html: Vec<Node>,
}

fn valid_name(name: &str) -> bool {
    let bytes = name.as_bytes();
    let first = bytes
        .first()
        .is_some_and(|b| b.is_ascii_lowercase() || b.is_ascii_digit());
    first
        && bytes.len() <= MAX_NAME_BYTES
        && bytes
            .iter()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || matches!(b, b'_' | b'-'))
}

/// The name as shown in an error: printable ASCII, cut to the limit.
fn shown(name: &str) -> String {
    name.chars()
        .take(MAX_NAME_BYTES)
        .map(|ch| if ch.is_ascii_graphic() { ch } else { '?' })
        .collect()
}

fn check_sizes(subject: &str, text: Option<&str>, html: &str) -> Result<(), Problem> {
    let parts = [
        (Part::Subject, subject.len(), MAX_SUBJECT_SOURCE_BYTES),
        (Part::Text, text.map_or(0, str::len), MAX_BODY_SOURCE_BYTES),
        (Part::Html, html.len(), MAX_BODY_SOURCE_BYTES),
    ];
    match parts.into_iter().find(|(_, size, limit)| size > limit) {
        Some((part, _, _)) => Err(Problem::new(Reason::TooLarge).within(part, None)),
        None => Ok(()),
    }
}

fn compile_part(src: &str, part: Part, budget: &mut Budget) -> Result<Vec<Node>, Problem> {
    let found = tokens(src, part)?;
    let found = if part == Part::Html {
        context::classify(found)?
    } else {
        found
    };
    parse(found, part, budget)
}

impl Template {
    /// Compiles the three parts. `text` is optional: without it the text is
    /// derived from the rendered html.
    pub fn compile(
        name: &str,
        subject: &str,
        text: Option<&str>,
        html: &str,
    ) -> Result<Self, TemplateError> {
        if !valid_name(name) {
            let problem = Problem::new(Reason::InvalidName);
            return Err(TemplateError::single(&shown(name), problem));
        }
        let fail = |problem| TemplateError::single(name, problem);
        check_sizes(subject, text, html).map_err(fail)?;
        let mut budget = Budget::new();
        let subject = compile_part(subject, Part::Subject, &mut budget).map_err(fail)?;
        let text = text
            .map(|src| compile_part(src, Part::Text, &mut budget))
            .transpose()
            .map_err(fail)?;
        let html = compile_part(html, Part::Html, &mut budget).map_err(fail)?;
        Ok(Self {
            name: name.to_owned(),
            subject,
            text,
            html,
        })
    }

    pub fn name(&self) -> &str {
        &self.name
    }

    /// Every problem between the template and the shape of its context, at
    /// most `MAX_PROBLEMS`.
    pub fn verify(&self, shape: &Shape) -> Result<(), TemplateError> {
        let mut parts: Vec<(Part, &[Node])> = vec![(Part::Subject, &self.subject)];
        if let Some(text) = &self.text {
            parts.push((Part::Text, text));
        }
        parts.push((Part::Html, &self.html));
        shape::verify(&self.name, &parts, shape)
    }

    /// Renders against `context` (a map), stopping at the first problem.
    pub fn render(&self, context: &Value) -> Result<Rendered, TemplateError> {
        let fail = |problem| TemplateError::single(&self.name, problem);
        if !matches!(context, Value::Map(_)) {
            return Err(fail(Problem::new(Reason::NotAnObject)));
        }
        let mut iterations = 0;
        let subject = render::render_part(&self.subject, context, Part::Subject, &mut iterations)
            .map_err(fail)?;
        let text = self
            .text
            .as_ref()
            .map(|nodes| render::render_part(nodes, context, Part::Text, &mut iterations))
            .transpose()
            .map_err(fail)?;
        let html =
            render::render_part(&self.html, context, Part::Html, &mut iterations).map_err(fail)?;
        let text = match text {
            Some(text) => text,
            None => text::derive(&html).map_err(fail)?,
        };
        Ok(Rendered {
            subject,
            text,
            html,
        })
    }
}
