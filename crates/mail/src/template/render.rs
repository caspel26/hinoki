//! Renders a node tree against a context. Output is bounded on every append;
//! recursion depth is bounded by the parser.

use super::context::safe_address;
use super::error::{Part, Pos, Problem, Reason};
use super::escape::{escape_html_into, refused_in_subject};
use super::lexer::Path;
use super::limits::{MAX_ITERATIONS, MAX_OUTPUT_BYTES, MAX_SUBJECT_BYTES};
use super::parser::Node;
use super::value::Value;

static NULL: Value = Value::Null;

struct Renderer<'a> {
    part: Part,
    root: &'a Value,
    scopes: Vec<(&'a str, &'a Value)>,
    out: String,
    limit: usize,
    iterations: &'a mut usize,
}

impl<'a> Renderer<'a> {
    fn problem(&self, reason: Reason, at: Pos, variable: Option<&Path>) -> Problem {
        let problem = Problem::new(reason).within(self.part, Some(at));
        match variable {
            Some(path) => problem.named(path.text()),
            None => problem,
        }
    }

    fn first(&self, name: &str) -> Option<&'a Value> {
        let scoped = self.scopes.iter().rev().find(|(var, _)| *var == name);
        if let Some((_, value)) = scoped {
            return Some(value);
        }
        match self.root {
            Value::Map(map) => map.get(name),
            _ => None,
        }
    }

    fn lookup(&self, path: &Path, at: Pos) -> Result<&'a Value, Problem> {
        let mut segments = path.segments.iter();
        let missing = || self.problem(Reason::MissingVariable, at, Some(path));
        let first = segments.next().ok_or_else(missing)?;
        let mut current = self.first(first).ok_or_else(missing)?;
        for segment in segments {
            current = match current {
                Value::Null => return Ok(&NULL),
                Value::Map(map) => map.get(segment).ok_or_else(missing)?,
                _ => return Err(self.problem(Reason::NotAnObject, at, Some(path))),
            };
        }
        Ok(current)
    }

    fn room(&self, extra: usize) -> bool {
        self.out.len().saturating_add(extra) <= self.limit
    }

    fn literal(&mut self, text: &str) -> Result<(), Reason> {
        if !self.room(text.len()) {
            return Err(Reason::OutputTooLarge);
        }
        self.out.push_str(text);
        Ok(())
    }

    /// Inserts a value: escaped in html, refused when it could break a header
    /// (subject) or carries a NUL (text and html).
    fn insert(&mut self, text: &str) -> Result<(), Reason> {
        let unsafe_text = match self.part {
            Part::Subject => text.chars().any(refused_in_subject),
            _ => text.contains('\0'),
        };
        if unsafe_text {
            return Err(match self.part {
                Part::Subject => Reason::HeaderInjection,
                _ => Reason::ControlCharacter,
            });
        }
        if !self.room(text.len()) {
            return Err(Reason::OutputTooLarge);
        }
        if self.part == Part::Html {
            escape_html_into(&mut self.out, text);
        } else {
            self.out.push_str(text);
        }
        if self.out.len() > self.limit {
            return Err(Reason::OutputTooLarge);
        }
        Ok(())
    }

    fn output(&mut self, path: &Path, at: Pos, url: bool) -> Result<(), Problem> {
        let value = self.lookup(path, at)?;
        let mut buffer = String::new();
        let text = value
            .scalar_text(&mut buffer)
            .map_err(|reason| self.problem(reason, at, Some(path)))?;
        if url && !safe_address(text) {
            return Err(self.problem(Reason::UnsafeUrl, at, Some(path)));
        }
        self.insert(text)
            .map_err(|reason| self.problem(reason, at, Some(path)))
    }

    fn branch(
        &mut self,
        cond: &Path,
        then: &'a [Node],
        otherwise: &'a [Node],
        at: Pos,
    ) -> Result<(), Problem> {
        let taken = self.lookup(cond, at)?.truthy();
        self.nodes(if taken { then } else { otherwise })
    }

    fn repeat(
        &mut self,
        var: &'a str,
        iter: &Path,
        body: &'a [Node],
        at: Pos,
    ) -> Result<(), Problem> {
        let items: &'a [Value] = match self.lookup(iter, at)? {
            Value::List(items) => items,
            Value::Null => &[],
            _ => return Err(self.problem(Reason::NotAList, at, Some(iter))),
        };
        if matches!(self.root, Value::Map(map) if map.get(var).is_some()) {
            return Err(self.problem(Reason::ShadowedName, at, None).named(var));
        }
        for item in items {
            *self.iterations += 1;
            if *self.iterations > MAX_ITERATIONS {
                return Err(self.problem(Reason::TooManyIterations, at, None));
            }
            self.scopes.push((var, item));
            let result = self.nodes(body);
            self.scopes.pop();
            result?;
        }
        Ok(())
    }

    fn nodes(&mut self, nodes: &'a [Node]) -> Result<(), Problem> {
        for node in nodes {
            match node {
                Node::Text(text) => self
                    .literal(text)
                    .map_err(|reason| Problem::new(reason).within(self.part, None))?,
                Node::Output { path, at, url } => self.output(path, *at, *url)?,
                Node::If {
                    cond,
                    then,
                    otherwise,
                    at,
                } => self.branch(cond, then, otherwise, *at)?,
                Node::For {
                    var,
                    iter,
                    body,
                    at,
                } => self.repeat(var, iter, body, *at)?,
            }
        }
        Ok(())
    }
}

/// The rendered text of one part; `iterations` is shared by all the parts of
/// one render.
pub(crate) fn render_part(
    nodes: &[Node],
    root: &Value,
    part: Part,
    iterations: &mut usize,
) -> Result<String, Problem> {
    let limit = if part == Part::Subject {
        MAX_SUBJECT_BYTES
    } else {
        MAX_OUTPUT_BYTES
    };
    let mut renderer = Renderer {
        part,
        root,
        scopes: Vec::new(),
        out: String::new(),
        limit,
        iterations,
    };
    renderer.nodes(nodes)?;
    Ok(renderer.out)
}
