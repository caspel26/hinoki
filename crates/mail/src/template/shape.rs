//! Checks a template against the shape of its context before anything is
//! rendered: every variable must exist, `{{ }}` must print a scalar, `for`
//! must walk a list, every field must be used and no field may be of a type
//! that cannot be shown.

use std::collections::HashSet;

use super::error::{Part, Pos, Problem, Problems, Reason, TemplateError};
use super::lexer::Path;
use super::limits::MAX_CONTEXT_DEPTH;
use super::parser::Node;

/// Why a field of the context cannot be used in a template.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Refusal {
    Secret,
    Bytes,
    Untyped,
    Mapping,
    Tuple,
    Union,
}

impl Refusal {
    pub fn parse(name: &str) -> Option<Refusal> {
        match name {
            "secret" => Some(Refusal::Secret),
            "bytes" => Some(Refusal::Bytes),
            "untyped" => Some(Refusal::Untyped),
            "mapping" => Some(Refusal::Mapping),
            "tuple" => Some(Refusal::Tuple),
            "union" => Some(Refusal::Union),
            _ => None,
        }
    }
}

/// What a context holds, field by field.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Shape {
    Scalar,
    List(Box<Shape>),
    Object(Vec<(String, Shape)>),
    Refused(Refusal),
}

impl Shape {
    /// Nesting levels, for a bound on what is walked.
    fn depth(&self) -> usize {
        match self {
            Shape::List(inner) => 1 + inner.depth(),
            Shape::Object(fields) => 1 + fields.iter().map(|(_, s)| s.depth()).max().unwrap_or(0),
            _ => 0,
        }
    }
}

static UNTYPED: Shape = Shape::Refused(Refusal::Untyped);

fn join(base: &str, name: &str) -> String {
    if base.is_empty() {
        name.to_owned()
    } else {
        format!("{base}.{name}")
    }
}

fn field<'a>(fields: &'a [(String, Shape)], name: &str) -> Option<&'a Shape> {
    fields
        .iter()
        .find(|(field, _)| field == name)
        .map(|(_, shape)| shape)
}

/// The paths that must be used: scalars and lists of scalars.
fn leaves(shape: &Shape, path: &str, found: &mut Vec<String>) {
    match shape {
        Shape::Scalar => found.push(path.to_owned()),
        Shape::List(inner) if **inner == Shape::Scalar => found.push(path.to_owned()),
        Shape::List(inner) => leaves(inner, &format!("{path}[]"), found),
        Shape::Object(fields) => {
            for (name, inner) in fields {
                leaves(inner, &join(path, name), found);
            }
        }
        Shape::Refused(_) => {}
    }
}

fn refusals(shape: &Shape, path: &str, problems: &mut Problems) {
    match shape {
        Shape::Refused(refusal) => {
            let reason = match refusal {
                Refusal::Secret => Reason::SecretField,
                _ => Reason::UnsupportedField,
            };
            problems.push(Problem::new(reason).named(path));
        }
        Shape::List(inner) => refusals(inner, &format!("{path}[]"), problems),
        Shape::Object(fields) => {
            for (name, inner) in fields {
                refusals(inner, &join(path, name), problems);
            }
        }
        Shape::Scalar => {}
    }
}

struct Scope<'a> {
    name: &'a str,
    shape: &'a Shape,
    path: String,
}

struct Verifier<'a> {
    root: &'a [(String, Shape)],
    scopes: Vec<Scope<'a>>,
    used: HashSet<String>,
    problems: Problems,
}

impl<'a> Verifier<'a> {
    fn report(&mut self, reason: Reason, part: Part, at: Pos, variable: &str) {
        self.problems
            .push(Problem::new(reason).within(part, Some(at)).named(variable));
    }

    fn start(&self, name: &str) -> Option<(&'a Shape, String)> {
        let scoped = self.scopes.iter().rev().find(|scope| scope.name == name);
        if let Some(scope) = scoped {
            return Some((scope.shape, scope.path.clone()));
        }
        field(self.root, name).map(|shape| (shape, name.to_owned()))
    }

    /// The shape a path leads to and its schema path. `None` when a problem
    /// was reported, or the path ends in a field already refused.
    fn resolve(&mut self, path: &Path, part: Part, at: Pos) -> Option<(&'a Shape, String)> {
        let (first, rest) = path.segments.split_first()?;
        let Some((mut shape, mut text)) = self.start(first) else {
            self.report(Reason::UnknownVariable, part, at, &path.text());
            return None;
        };
        for segment in rest {
            let Shape::Object(fields) = shape else {
                if !matches!(shape, Shape::Refused(_)) {
                    self.report(Reason::NotAnObject, part, at, &path.text());
                }
                return None;
            };
            let Some(next) = field(fields, segment) else {
                self.report(Reason::UnknownVariable, part, at, &path.text());
                return None;
            };
            shape = next;
            text = join(&text, segment);
        }
        Some((shape, text))
    }

    fn output(&mut self, path: &Path, part: Part, at: Pos) {
        let Some((shape, text)) = self.resolve(path, part, at) else {
            return;
        };
        match shape {
            Shape::Scalar => {
                self.used.insert(text);
            }
            Shape::Refused(_) => {}
            _ => self.report(Reason::NotText, part, at, &path.text()),
        }
    }

    fn condition(&mut self, path: &Path, part: Part, at: Pos) {
        if let Some((_, text)) = self.resolve(path, part, at) {
            self.used.insert(text);
        }
    }

    fn repeat(&mut self, var: &'a str, iter: &Path, body: &'a [Node], part: Part, at: Pos) {
        if field(self.root, var).is_some() {
            self.report(Reason::ShadowedName, part, at, var);
        }
        let (item, path) = match self.resolve(iter, part, at) {
            Some((Shape::List(inner), text)) => {
                self.used.insert(text.clone());
                (&**inner, format!("{text}[]"))
            }
            Some((Shape::Refused(_), text)) => (&UNTYPED, text),
            Some((_, text)) => {
                self.report(Reason::NotAList, part, at, &iter.text());
                (&UNTYPED, text)
            }
            None => (&UNTYPED, String::new()),
        };
        self.scopes.push(Scope {
            name: var,
            shape: item,
            path,
        });
        self.nodes(body, part);
        self.scopes.pop();
    }

    fn nodes(&mut self, nodes: &'a [Node], part: Part) {
        for node in nodes {
            match node {
                Node::Text(_) => {}
                Node::Output { path, at, .. } => self.output(path, part, *at),
                Node::If {
                    cond,
                    then,
                    otherwise,
                    at,
                } => {
                    self.condition(cond, part, *at);
                    self.nodes(then, part);
                    self.nodes(otherwise, part);
                }
                Node::For {
                    var,
                    iter,
                    body,
                    at,
                } => self.repeat(var, iter, body, part, *at),
            }
        }
    }
}

/// Verifies the parts of a template (`parts`, in order) against `shape`.
pub(crate) fn verify(
    template: &str,
    parts: &[(Part, &[Node])],
    shape: &Shape,
) -> Result<(), TemplateError> {
    let Shape::Object(root) = shape else {
        return Err(TemplateError::single(
            template,
            Problem::new(Reason::UnsupportedField),
        ));
    };
    if shape.depth() > MAX_CONTEXT_DEPTH {
        return Err(TemplateError::single(
            template,
            Problem::new(Reason::TooDeep),
        ));
    }
    let mut verifier = Verifier {
        root,
        scopes: Vec::new(),
        used: HashSet::new(),
        problems: Problems::default(),
    };
    for (part, nodes) in parts {
        verifier.nodes(nodes, *part);
    }
    refusals(shape, "", &mut verifier.problems);
    let mut expected = Vec::new();
    leaves(shape, "", &mut expected);
    for path in expected {
        if !verifier.used.contains(&path) {
            verifier
                .problems
                .push(Problem::new(Reason::UnusedField).named(path));
        }
    }
    verifier.problems.finish(template)
}
