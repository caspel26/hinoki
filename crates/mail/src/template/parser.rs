//! Builds the node tree from tokens with an explicit stack of open blocks, so
//! nesting never recurses.

use super::error::{Part, Pos, Problem, Reason};
use super::lexer::{Path, Tag, Token};
use super::limits::{MAX_DEPTH, MAX_NODES};

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum Node {
    Text(String),
    Output {
        path: Path,
        at: Pos,
        url: bool,
    },
    If {
        cond: Path,
        then: Vec<Node>,
        otherwise: Vec<Node>,
        at: Pos,
    },
    For {
        var: String,
        iter: Path,
        body: Vec<Node>,
        at: Pos,
    },
}

/// Nodes still allowed in the whole template (all its parts).
pub(crate) struct Budget {
    left: usize,
}

impl Budget {
    pub fn new() -> Self {
        Self { left: MAX_NODES }
    }

    fn take(&mut self) -> bool {
        match self.left.checked_sub(1) {
            Some(left) => {
                self.left = left;
                true
            }
            None => false,
        }
    }
}

enum Open {
    If(Path),
    For { var: String, iter: Path },
}

/// A block being read: what opened it, the nodes of its current branch and,
/// for an `if` past its `else`, the nodes of the first branch.
struct Frame {
    open: Open,
    at: Pos,
    nodes: Vec<Node>,
    then: Option<Vec<Node>>,
}

struct Parser<'a> {
    part: Part,
    budget: &'a mut Budget,
    root: Vec<Node>,
    stack: Vec<Frame>,
}

impl Parser<'_> {
    fn problem(&self, reason: Reason, at: Pos) -> Problem {
        Problem::new(reason).within(self.part, Some(at))
    }

    fn charge(&mut self, at: Option<Pos>) -> Result<(), Problem> {
        if self.budget.take() {
            return Ok(());
        }
        Err(Problem::new(Reason::TooManyNodes).within(self.part, at))
    }

    fn current(&mut self) -> &mut Vec<Node> {
        match self.stack.last_mut() {
            Some(frame) => &mut frame.nodes,
            None => &mut self.root,
        }
    }

    fn open(&mut self, open: Open, at: Pos) -> Result<(), Problem> {
        if self.stack.len() >= MAX_DEPTH {
            return Err(self.problem(Reason::TooDeep, at));
        }
        if let Open::For { var, .. } = &open {
            let taken = self
                .stack
                .iter()
                .any(|frame| matches!(&frame.open, Open::For { var: outer, .. } if outer == var));
            if taken {
                return Err(self.problem(Reason::ShadowedName, at).named(var.as_str()));
            }
        }
        self.stack.push(Frame {
            open,
            at,
            nodes: Vec::new(),
            then: None,
        });
        Ok(())
    }

    fn otherwise(&mut self, at: Pos) -> Result<(), Problem> {
        match self.stack.last_mut() {
            Some(frame) if matches!(frame.open, Open::If(_)) && frame.then.is_none() => {
                frame.then = Some(std::mem::take(&mut frame.nodes));
                Ok(())
            }
            _ => Err(self.problem(Reason::UnexpectedTag, at)),
        }
    }

    fn close(&mut self, closes_if: bool, at: Pos) -> Result<(), Problem> {
        let matching = self
            .stack
            .last()
            .is_some_and(|frame| matches!(frame.open, Open::If(_)) == closes_if);
        if !matching {
            return Err(self.problem(Reason::UnexpectedTag, at));
        }
        let Some(frame) = self.stack.pop() else {
            return Err(self.problem(Reason::UnexpectedTag, at));
        };
        let node = match frame.open {
            Open::If(cond) => {
                let (then, otherwise) = match frame.then {
                    Some(then) => (then, frame.nodes),
                    None => (frame.nodes, Vec::new()),
                };
                Node::If {
                    cond,
                    then,
                    otherwise,
                    at: frame.at,
                }
            }
            Open::For { var, iter } => Node::For {
                var,
                iter,
                body: frame.nodes,
                at: frame.at,
            },
        };
        self.current().push(node);
        Ok(())
    }

    fn tag(&mut self, tag: Tag, at: Pos) -> Result<(), Problem> {
        match tag {
            Tag::If(cond) => {
                self.charge(Some(at))?;
                self.open(Open::If(cond), at)
            }
            Tag::For { var, iter } => {
                self.charge(Some(at))?;
                self.open(Open::For { var, iter }, at)
            }
            Tag::Else => self.otherwise(at),
            Tag::EndIf => self.close(true, at),
            Tag::EndFor => self.close(false, at),
        }
    }

    fn token(&mut self, token: Token) -> Result<(), Problem> {
        match token {
            Token::Text(text) => {
                self.charge(None)?;
                self.current().push(Node::Text(text));
                Ok(())
            }
            Token::Output { path, at, url } => {
                self.charge(Some(at))?;
                self.current().push(Node::Output { path, at, url });
                Ok(())
            }
            Token::Tag { tag, at } => self.tag(tag, at),
        }
    }

    fn finish(mut self) -> Result<Vec<Node>, Problem> {
        if let Some(frame) = self.stack.pop() {
            return Err(self.problem(Reason::UnclosedBlock, frame.at));
        }
        Ok(self.root)
    }
}

pub(crate) fn parse(
    tokens: Vec<Token>,
    part: Part,
    budget: &mut Budget,
) -> Result<Vec<Node>, Problem> {
    let mut parser = Parser {
        part,
        budget,
        root: Vec::new(),
        stack: Vec::new(),
    };
    for token in tokens {
        parser.token(token)?;
    }
    parser.finish()
}

#[cfg(test)]
mod tests {
    use super::super::lexer::tokens;
    use super::*;

    fn build(src: &str) -> Result<Vec<Node>, Problem> {
        parse(tokens(src, Part::Html)?, Part::Html, &mut Budget::new())
    }

    fn reason(src: &str) -> Reason {
        build(src).unwrap_err().reason
    }

    #[test]
    fn nests_blocks_and_both_branches_of_an_if() {
        let nodes =
            build("{% if a %}x{% else %}y{% endif %}{% for i in l %}{{ i }}{% endfor %}").unwrap();
        assert_eq!(nodes.len(), 2);
        let Node::If {
            then, otherwise, ..
        } = &nodes[0]
        else {
            panic!("expected an if");
        };
        assert_eq!((then.len(), otherwise.len()), (1, 1));
        let Node::For { var, body, .. } = &nodes[1] else {
            panic!("expected a for");
        };
        assert_eq!((var.as_str(), body.len()), ("i", 1));
        let nodes = build("{% if a %}{% else %}z{% endif %}").unwrap();
        assert!(
            matches!(&nodes[0], Node::If { then, otherwise, .. } if then.is_empty() && otherwise.len() == 1)
        );
    }

    #[test]
    fn misplaced_tags_are_unexpected() {
        for src in [
            "{% endif %}",
            "{% endfor %}",
            "{% else %}",
            "{% for i in l %}{% endif %}",
            "{% if a %}{% endfor %}",
            "{% for i in l %}{% else %}{% endfor %}",
            "{% if a %}{% else %}{% else %}{% endif %}",
        ] {
            assert_eq!(reason(src), Reason::UnexpectedTag, "{src}");
        }
    }

    #[test]
    fn an_unclosed_block_is_reported_where_it_opened() {
        let error = build("a\n{% if a %}\n{% for i in l %}").unwrap_err();
        assert_eq!(error.reason, Reason::UnclosedBlock);
        assert_eq!(error.at, Some(Pos { line: 3, column: 4 }));
    }

    #[test]
    fn depth_sixteen_is_allowed_and_seventeen_is_not() {
        let nest = |depth: usize| {
            format!(
                "{}x{}",
                "{% if a %}".repeat(depth),
                "{% endif %}".repeat(depth)
            )
        };
        assert!(build(&nest(MAX_DEPTH)).is_ok());
        assert_eq!(reason(&nest(MAX_DEPTH + 1)), Reason::TooDeep);
    }

    #[test]
    fn nodes_are_limited_across_parts() {
        let outputs = |count: usize| "{{ a }}".repeat(count);
        assert!(build(&outputs(MAX_NODES)).is_ok());
        assert_eq!(reason(&outputs(MAX_NODES + 1)), Reason::TooManyNodes);
        let mut budget = Budget::new();
        let first = tokens(&outputs(MAX_NODES), Part::Html).unwrap();
        parse(first, Part::Html, &mut budget).unwrap();
        let more = tokens("text", Part::Text).unwrap();
        let error = parse(more, Part::Text, &mut budget).unwrap_err();
        assert_eq!(
            (error.reason, error.part),
            (Reason::TooManyNodes, Some(Part::Text))
        );
        let blocks = "{% if a %}{% endif %}".repeat(MAX_NODES + 1);
        assert_eq!(reason(&blocks), Reason::TooManyNodes);
    }

    #[test]
    fn a_loop_variable_cannot_shadow_an_enclosing_one() {
        let error = build("{% for a in l %}{% for a in m %}{% endfor %}{% endfor %}").unwrap_err();
        assert_eq!(error.reason, Reason::ShadowedName);
        assert_eq!(error.variable.as_deref(), Some("a"));
        assert!(build("{% for a in l %}{% endfor %}{% for a in l %}{% endfor %}").is_ok());
    }
}
