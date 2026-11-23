//! Template problems. A problem names a reason, the part and the position in
//! the template source, and a variable path taken from the template source:
//! never a value of the context, a recipient or rendered text.

use std::fmt;

use super::limits::MAX_PROBLEMS;
use crate::error::{Field, Kind, MailError};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Part {
    Subject,
    Text,
    Html,
}

impl Part {
    pub fn as_str(self) -> &'static str {
        match self {
            Part::Subject => "subject",
            Part::Text => "text",
            Part::Html => "html",
        }
    }

    pub fn parse(name: &str) -> Option<Part> {
        [Part::Subject, Part::Text, Part::Html]
            .into_iter()
            .find(|part| part.as_str() == name)
    }

    pub fn field(self) -> Field {
        match self {
            Part::Subject => Field::Subject,
            Part::Text => Field::Text,
            Part::Html => Field::Html,
        }
    }
}

/// A position in the template source, 1-based; the column counts characters.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Pos {
    pub line: u32,
    pub column: u32,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Reason {
    Syntax,
    UnclosedTag,
    UnexpectedTag,
    UnclosedBlock,
    InvalidPath,
    ShadowedName,
    ControlCharacter,
    TooLarge,
    TooManyNodes,
    TooDeep,
    InvalidName,
    UnknownVariable,
    UnusedField,
    NotText,
    NotAList,
    NotAnObject,
    SecretField,
    UnsupportedField,
    MissingVariable,
    HeaderInjection,
    OutputTooLarge,
    TooManyIterations,
    ContextTooLarge,
    UnsupportedValue,
    SecretValue,
    Unreadable,
    InvalidFile,
    UnsafeContext,
    UnsafeUrl,
}

impl Reason {
    pub const ALL: [Reason; 29] = [
        Reason::Syntax,
        Reason::UnclosedTag,
        Reason::UnexpectedTag,
        Reason::UnclosedBlock,
        Reason::InvalidPath,
        Reason::ShadowedName,
        Reason::ControlCharacter,
        Reason::TooLarge,
        Reason::TooManyNodes,
        Reason::TooDeep,
        Reason::InvalidName,
        Reason::UnknownVariable,
        Reason::UnusedField,
        Reason::NotText,
        Reason::NotAList,
        Reason::NotAnObject,
        Reason::SecretField,
        Reason::UnsupportedField,
        Reason::MissingVariable,
        Reason::HeaderInjection,
        Reason::OutputTooLarge,
        Reason::TooManyIterations,
        Reason::ContextTooLarge,
        Reason::UnsupportedValue,
        Reason::SecretValue,
        Reason::Unreadable,
        Reason::InvalidFile,
        Reason::UnsafeContext,
        Reason::UnsafeUrl,
    ];

    /// The stable snake_case name (the value of `TemplateReason`).
    pub fn as_str(self) -> &'static str {
        match self {
            Reason::Syntax => "syntax",
            Reason::UnclosedTag => "unclosed_tag",
            Reason::UnexpectedTag => "unexpected_tag",
            Reason::UnclosedBlock => "unclosed_block",
            Reason::InvalidPath => "invalid_path",
            Reason::ShadowedName => "shadowed_name",
            Reason::ControlCharacter => "control_character",
            Reason::TooLarge => "too_large",
            Reason::TooManyNodes => "too_many_nodes",
            Reason::TooDeep => "too_deep",
            Reason::InvalidName => "invalid_name",
            Reason::UnknownVariable => "unknown_variable",
            Reason::UnusedField => "unused_field",
            Reason::NotText => "not_text",
            Reason::NotAList => "not_a_list",
            Reason::NotAnObject => "not_an_object",
            Reason::SecretField => "secret_field",
            Reason::UnsupportedField => "unsupported_field",
            Reason::MissingVariable => "missing_variable",
            Reason::HeaderInjection => "header_injection",
            Reason::OutputTooLarge => "output_too_large",
            Reason::TooManyIterations => "too_many_iterations",
            Reason::ContextTooLarge => "context_too_large",
            Reason::UnsupportedValue => "unsupported_value",
            Reason::SecretValue => "secret_value",
            Reason::Unreadable => "unreadable",
            Reason::InvalidFile => "invalid_file",
            Reason::UnsafeContext => "unsafe_context",
            Reason::UnsafeUrl => "unsafe_url",
        }
    }

    pub fn parse(name: &str) -> Option<Reason> {
        Reason::ALL
            .into_iter()
            .find(|reason| reason.as_str() == name)
    }

    /// A fixed phrase; a variable path, when there is one, follows it.
    fn phrase(self) -> &'static str {
        match self {
            Reason::Syntax => "invalid syntax",
            Reason::UnclosedTag => "unclosed tag",
            Reason::UnexpectedTag => "unexpected tag",
            Reason::UnclosedBlock => "unclosed block",
            Reason::InvalidPath => "invalid variable name",
            Reason::ShadowedName => "loop variable shadows",
            Reason::ControlCharacter => "control character",
            Reason::TooLarge => "template source is too large",
            Reason::TooManyNodes => "too many tags and text runs",
            Reason::TooDeep => "blocks or fields nested too deeply",
            Reason::InvalidName => "invalid template name",
            Reason::UnknownVariable => "unknown variable",
            Reason::UnusedField => "unused field",
            Reason::NotText => "cannot print list or object",
            Reason::NotAList => "cannot loop over non-list",
            Reason::NotAnObject => "cannot read a field of non-object",
            Reason::SecretField => "secret field",
            Reason::UnsupportedField => "unsupported field type",
            Reason::MissingVariable => "missing variable",
            Reason::HeaderInjection => "line break or control character in subject value",
            Reason::OutputTooLarge => "output is too large",
            Reason::TooManyIterations => "too many loop iterations",
            Reason::ContextTooLarge => "context is too large",
            Reason::UnsupportedValue => "unsupported value",
            Reason::SecretValue => "secret value",
            Reason::Unreadable => "template file cannot be read",
            Reason::InvalidFile => "invalid template file",
            Reason::UnsafeContext => "value placed where escaping is not enough",
            Reason::UnsafeUrl => "address with a scheme that is not http, https, mailto or tel",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Problem {
    pub reason: Reason,
    pub part: Option<Part>,
    pub at: Option<Pos>,
    pub variable: Option<String>,
}

impl Problem {
    pub fn new(reason: Reason) -> Self {
        Self {
            reason,
            part: None,
            at: None,
            variable: None,
        }
    }

    pub fn within(mut self, part: Part, at: Option<Pos>) -> Self {
        self.part = Some(part);
        self.at = at;
        self
    }

    pub fn named(mut self, variable: impl Into<String>) -> Self {
        self.variable = Some(variable.into());
        self
    }
}

impl fmt::Display for Problem {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.reason.phrase())?;
        if let Some(variable) = &self.variable {
            write!(f, " '{variable}'")?;
        }
        if let Some(part) = self.part {
            write!(f, " in {}", part.as_str())?;
        }
        if let Some(at) = self.at {
            write!(f, " at line {}, column {}", at.line, at.column)?;
        }
        Ok(())
    }
}

/// What went wrong with one template: at least one problem, at most
/// `MAX_PROBLEMS`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TemplateError {
    pub template: String,
    pub problems: Vec<Problem>,
    /// More problems were found than are listed.
    pub truncated: bool,
}

impl TemplateError {
    pub fn single(template: &str, problem: Problem) -> Self {
        Self {
            template: template.to_owned(),
            problems: vec![problem],
            truncated: false,
        }
    }
}

impl fmt::Display for TemplateError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "template '{}'", self.template)?;
        let Some((first, rest)) = self.problems.split_first() else {
            return Ok(());
        };
        write!(f, ": {first}")?;
        match (rest.len(), self.truncated) {
            (0, false) => Ok(()),
            (_, true) => f.write_str(" (and more problems)"),
            (1, false) => f.write_str(" (and 1 more problem)"),
            (more, false) => write!(f, " (and {more} more problems)"),
        }
    }
}

impl std::error::Error for TemplateError {}

impl From<&TemplateError> for MailError {
    fn from(error: &TemplateError) -> Self {
        let part = error.problems.first().and_then(|problem| problem.part);
        match part {
            Some(part) => MailError::at(Kind::Template, part.field(), None),
            None => MailError::new(Kind::Template),
        }
    }
}

/// Collects problems up to the limit and remembers that some were dropped.
#[derive(Default)]
pub(crate) struct Problems {
    pub list: Vec<Problem>,
    pub truncated: bool,
}

impl Problems {
    pub fn push(&mut self, problem: Problem) {
        if self.list.len() < MAX_PROBLEMS {
            self.list.push(problem);
        } else {
            self.truncated = true;
        }
    }

    pub fn finish(self, template: &str) -> Result<(), TemplateError> {
        if self.list.is_empty() {
            return Ok(());
        }
        Err(TemplateError {
            template: template.to_owned(),
            problems: self.list,
            truncated: self.truncated,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_reason_has_a_distinct_name_that_parses_back() {
        let mut names: Vec<_> = Reason::ALL.iter().map(|r| r.as_str()).collect();
        for reason in Reason::ALL {
            assert_eq!(Reason::parse(reason.as_str()), Some(reason));
            assert!(!Problem::new(reason).to_string().is_empty());
        }
        names.sort_unstable();
        names.dedup();
        assert_eq!(names.len(), Reason::ALL.len());
        assert_eq!(Reason::parse("nope"), None);
    }

    #[test]
    fn parts_round_trip_and_map_to_message_fields() {
        for part in [Part::Subject, Part::Text, Part::Html] {
            assert_eq!(Part::parse(part.as_str()), Some(part));
        }
        assert_eq!(Part::parse("body"), None);
        assert_eq!(Part::Html.field(), Field::Html);
        assert_eq!(Part::Text.field(), Field::Text);
        assert_eq!(Part::Subject.field(), Field::Subject);
    }

    #[test]
    fn display_names_variable_part_and_position() {
        let problem = Problem::new(Reason::UnknownVariable)
            .within(Part::Html, Some(Pos { line: 4, column: 9 }))
            .named("user.nickname");
        assert_eq!(
            problem.to_string(),
            "unknown variable 'user.nickname' in html at line 4, column 9"
        );
        assert_eq!(
            Problem::new(Reason::UnusedField).to_string(),
            "unused field"
        );
    }

    #[test]
    fn an_error_counts_the_remaining_problems() {
        let problem = || Problem::new(Reason::Syntax).within(Part::Subject, None);
        let mut error = TemplateError::single("welcome", problem());
        assert_eq!(
            error.to_string(),
            "template 'welcome': invalid syntax in subject"
        );
        error.problems.push(problem());
        assert!(error.to_string().ends_with("(and 1 more problem)"));
        error.problems.push(problem());
        assert!(error.to_string().ends_with("(and 2 more problems)"));
        error.truncated = true;
        assert!(error.to_string().ends_with("(and more problems)"));
        let mail = MailError::from(&error);
        assert_eq!(
            (mail.kind, mail.field),
            (Kind::Template, Some(Field::Subject))
        );
        let empty = TemplateError {
            template: "x".to_owned(),
            problems: vec![],
            truncated: false,
        };
        assert_eq!(MailError::from(&empty).field, None);
        assert_eq!(empty.to_string(), "template 'x'");
    }

    #[test]
    fn problems_stop_at_the_limit() {
        let mut problems = Problems::default();
        for _ in 0..MAX_PROBLEMS + 3 {
            problems.push(Problem::new(Reason::Syntax));
        }
        let error = problems.finish("t").unwrap_err();
        assert_eq!(error.problems.len(), MAX_PROBLEMS);
        assert!(error.truncated);
        assert!(Problems::default().finish("t").is_ok());
    }
}
