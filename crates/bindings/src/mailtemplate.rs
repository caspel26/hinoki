//! Python bindings of the email template engine: `MailTemplate` compiles the
//! three parts, checks them against the shape of a typed context, and renders
//! a context made of plain Python values. The rendering itself runs without
//! the GIL on a copy of the context.

use std::sync::Arc;

use hinoki_mail::template_limits::{
    MAX_CONTEXT_BYTES, MAX_CONTEXT_DEPTH, MAX_CONTEXT_NODES, MAX_IDENT_BYTES,
};
use hinoki_mail::{Part, Pos, Problem, Reason, Refusal, Shape, Template, TemplateError, Value};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};

use crate::pytypes::secret_class;

static TEMPLATE_ERROR: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static SETTINGS_SECRET: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

/// The longest variable path an error shows, in characters.
const MAX_SHOWN_PATH: usize = 8 * MAX_IDENT_BYTES;

/// What has been read of a context so far: the budget and the keys on the way
/// down, for the path of an unsupported value.
#[derive(Default)]
struct Walk {
    nodes: usize,
    bytes: usize,
    path: Vec<String>,
}

impl Walk {
    fn enter(&mut self, depth: usize) -> Result<(), Problem> {
        self.nodes += 1;
        if depth > MAX_CONTEXT_DEPTH || self.nodes > MAX_CONTEXT_NODES {
            return Err(Problem::new(Reason::ContextTooLarge));
        }
        Ok(())
    }

    fn add_bytes(&mut self, size: usize) -> Result<(), Problem> {
        self.bytes = self.bytes.saturating_add(size);
        if self.bytes > MAX_CONTEXT_BYTES {
            return Err(Problem::new(Reason::ContextTooLarge));
        }
        Ok(())
    }

    fn problem(&self, reason: Reason) -> Problem {
        let mut shown: String = self
            .path
            .join(".")
            .chars()
            .map(|c| if c.is_control() { '?' } else { c })
            .collect();
        if let Some((cut, _)) = shown.char_indices().nth(MAX_SHOWN_PATH) {
            shown.truncate(cut);
        }
        let problem = Problem::new(reason);
        if shown.is_empty() {
            problem
        } else {
            problem.named(shown)
        }
    }
}

fn is_secret(value: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = value.py();
    let settings = SETTINGS_SECRET.import(py, "hinoki.settings", "Secret")?;
    Ok(value.is_instance(secret_class(py)?)? || value.is_instance(settings)?)
}

/// An `int`: a number when it fits 64 bits, else its decimal digits as text
/// (Python refuses to write more than its own digit limit).
fn int_of(value: &Bound<'_, PyAny>, walk: &mut Walk) -> Result<Value, Problem> {
    if let Ok(number) = value.extract::<i64>() {
        return Ok(Value::Int(number));
    }
    let digits = value
        .str()
        .and_then(|text| text.to_str().map(str::to_owned))
        .map_err(|_| walk.problem(Reason::UnsupportedValue))?;
    walk.add_bytes(digits.len())?;
    Ok(Value::Str(digits))
}

fn scalar_of(value: &Bound<'_, PyAny>, walk: &mut Walk) -> Result<Option<Value>, Problem> {
    if value.is_none() {
        return Ok(Some(Value::Null));
    }
    if let Ok(flag) = value.cast::<PyBool>() {
        return Ok(Some(Value::Bool(flag.is_true())));
    }
    if value.is_instance_of::<PyInt>() {
        return int_of(value, walk).map(Some);
    }
    if let Ok(float) = value.cast::<PyFloat>() {
        let number = float.value();
        if !number.is_finite() {
            return Err(walk.problem(Reason::UnsupportedValue));
        }
        return Ok(Some(Value::Float(number)));
    }
    if let Ok(text) = value.cast::<PyString>() {
        let text = text
            .to_str()
            .map_err(|_| walk.problem(Reason::UnsupportedValue))?;
        walk.add_bytes(text.len())?;
        return Ok(Some(Value::Str(text.to_owned())));
    }
    Ok(None)
}

fn mapping_of(dict: &Bound<'_, PyDict>, walk: &mut Walk, depth: usize) -> Result<Value, Problem> {
    let mut entries = Vec::with_capacity(dict.len().min(MAX_CONTEXT_NODES));
    for (key, item) in dict.iter() {
        let Ok(name) = key.cast::<PyString>() else {
            return Err(walk.problem(Reason::UnsupportedValue));
        };
        let name = name
            .to_str()
            .map_err(|_| walk.problem(Reason::UnsupportedValue))?
            .to_owned();
        walk.add_bytes(name.len())?;
        walk.path.push(name.clone());
        let value = value_of(&item, walk, depth + 1);
        walk.path.pop();
        entries.push((name, value?));
    }
    Ok(Value::map(entries))
}

fn items_of<'py>(
    items: impl Iterator<Item = Bound<'py, PyAny>>,
    walk: &mut Walk,
    depth: usize,
) -> Result<Value, Problem> {
    items
        .map(|item| value_of(&item, walk, depth + 1))
        .collect::<Result<Vec<_>, _>>()
        .map(Value::List)
}

fn value_of(value: &Bound<'_, PyAny>, walk: &mut Walk, depth: usize) -> Result<Value, Problem> {
    walk.enter(depth)?;
    if let Some(scalar) = scalar_of(value, walk)? {
        return Ok(scalar);
    }
    if let Ok(dict) = value.cast::<PyDict>() {
        return mapping_of(dict, walk, depth);
    }
    if let Ok(list) = value.cast::<PyList>() {
        return items_of(list.iter(), walk, depth);
    }
    if let Ok(tuple) = value.cast::<PyTuple>() {
        return items_of(tuple.iter(), walk, depth);
    }
    let reason = match is_secret(value) {
        Ok(true) => Reason::SecretValue,
        _ => Reason::UnsupportedValue,
    };
    Err(walk.problem(reason))
}

fn node_error(message: &str) -> PyErr {
    PyValueError::new_err(format!("invalid template shape: {message}"))
}

fn shape_of(node: &Bound<'_, PyAny>, depth: usize) -> PyResult<Shape> {
    if depth > MAX_CONTEXT_DEPTH {
        return Err(node_error("nested too deeply"));
    }
    if let Ok(text) = node.cast::<PyString>() {
        return match text.to_str()? {
            "s" => Ok(Shape::Scalar),
            _ => Err(node_error("unknown scalar")),
        };
    }
    if let Ok(pair) = node.cast::<PyTuple>() {
        return tagged_shape(pair, depth);
    }
    if let Ok(dict) = node.cast::<PyDict>() {
        return object_shape(dict, depth);
    }
    Err(node_error("unexpected node"))
}

fn tagged_shape(pair: &Bound<'_, PyTuple>, depth: usize) -> PyResult<Shape> {
    if pair.len() != 2 {
        return Err(node_error("a tagged node has two items"));
    }
    let tag: String = pair.get_item(0)?.extract()?;
    let body = pair.get_item(1)?;
    match tag.as_str() {
        "l" => Ok(Shape::List(Box::new(shape_of(&body, depth + 1)?))),
        "x" => {
            let name: String = body.extract()?;
            Refusal::parse(&name)
                .map(Shape::Refused)
                .ok_or_else(|| node_error("unknown refusal"))
        }
        _ => Err(node_error("unknown tag")),
    }
}

fn object_shape(dict: &Bound<'_, PyDict>, depth: usize) -> PyResult<Shape> {
    let mut fields = Vec::with_capacity(dict.len());
    for (key, node) in dict.iter() {
        fields.push((key.extract::<String>()?, shape_of(&node, depth + 1)?));
    }
    Ok(Shape::Object(fields))
}

type ProblemTuple = (
    &'static str,
    Option<&'static str>,
    Option<u32>,
    Option<u32>,
    Option<String>,
);

fn problem_tuple(problem: &Problem) -> ProblemTuple {
    (
        problem.reason.as_str(),
        problem.part.map(Part::as_str),
        problem.at.map(|at| at.line),
        problem.at.map(|at| at.column),
        problem.variable.clone(),
    )
}

/// The `hinoki.mail.TemplateError` for `error`, built from plain attributes.
fn template_error(py: Python<'_>, error: &TemplateError) -> PyErr {
    build_error(py, error).unwrap_or_else(|failure| failure)
}

fn build_error(py: Python<'_>, error: &TemplateError) -> PyResult<PyErr> {
    let builder = TEMPLATE_ERROR.import(py, "hinoki.mail", "template_error")?;
    let problems: Vec<ProblemTuple> = error.problems.iter().map(problem_tuple).collect();
    let instance = builder.call1((&error.template, problems, error.truncated))?;
    Ok(PyErr::from_value(instance))
}

/// A compiled template. Build it once (at import or at startup), render it for
/// every message.
#[pyclass(name = "MailTemplate", module = "hinoki.core", frozen)]
pub(crate) struct PyMailTemplate {
    inner: Arc<Template>,
}

#[pymethods]
impl PyMailTemplate {
    /// Compile the parts and, with a `shape`, check every variable against it.
    #[new]
    #[pyo3(signature = (name, subject, html, text=None, shape=None))]
    fn new(
        py: Python<'_>,
        name: &str,
        subject: &str,
        html: &str,
        text: Option<&str>,
        shape: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Self> {
        let template = Template::compile(name, subject, text, html)
            .map_err(|error| template_error(py, &error))?;
        if let Some(shape) = shape {
            let shape = object_shape(shape, 0)?;
            template
                .verify(&shape)
                .map_err(|error| template_error(py, &error))?;
        }
        Ok(Self {
            inner: Arc::new(template),
        })
    }

    /// `(subject, text, html)`; the text is derived from the html when the
    /// template has none.
    fn render(
        &self,
        py: Python<'_>,
        context: &Bound<'_, PyAny>,
    ) -> PyResult<(String, String, String)> {
        let mut walk = Walk::default();
        let value = value_of(context, &mut walk, 0).map_err(|problem| {
            template_error(py, &TemplateError::single(self.inner.name(), problem))
        })?;
        let template = Arc::clone(&self.inner);
        let rendered = py
            .detach(|| template.render(&value))
            .map_err(|error| template_error(py, &error))?;
        Ok((rendered.subject, rendered.text, rendered.html))
    }
}

/// A problem as Python hands it over: reason, part, line, column, variable.
type PlainProblem = (
    String,
    Option<String>,
    Option<u32>,
    Option<u32>,
    Option<String>,
);

/// The text of a template error as the engine words it:
/// `template 'x': unknown variable 'a.b' in html at line 2, column 4`.
#[pyfunction]
#[pyo3(signature = (template, problems, truncated))]
pub(crate) fn describe_template_error(
    template: &str,
    problems: Vec<PlainProblem>,
    truncated: bool,
) -> PyResult<String> {
    let mut listed = Vec::with_capacity(problems.len());
    for (reason, part, line, column, variable) in problems {
        let reason = Reason::parse(&reason).ok_or_else(|| node_error("unknown reason"))?;
        let mut problem = Problem::new(reason);
        problem.part = part.as_deref().and_then(Part::parse);
        problem.at = line.zip(column).map(|(line, column)| Pos { line, column });
        problem.variable = variable;
        listed.push(problem);
    }
    let error = TemplateError {
        template: template.to_owned(),
        problems: listed,
        truncated,
    };
    Ok(error.to_string())
}

/// Every reason name the engine knows, for the Python enum to be checked
/// against.
#[pyfunction]
pub(crate) fn template_reasons() -> Vec<&'static str> {
    Reason::ALL.iter().map(|reason| reason.as_str()).collect()
}

/// The fixed size limits of the engine, by name.
#[pyfunction]
pub(crate) fn template_limits() -> Vec<(&'static str, usize)> {
    use hinoki_mail::template_limits as limits;
    vec![
        ("max_subject_source_bytes", limits::MAX_SUBJECT_SOURCE_BYTES),
        ("max_body_source_bytes", limits::MAX_BODY_SOURCE_BYTES),
        ("max_file_bytes", limits::MAX_FILE_BYTES),
        ("max_nodes", limits::MAX_NODES),
        ("max_depth", limits::MAX_DEPTH),
        ("max_path_segments", limits::MAX_PATH_SEGMENTS),
        ("max_ident_bytes", limits::MAX_IDENT_BYTES),
        ("max_subject_bytes", limits::MAX_SUBJECT_BYTES),
        ("max_output_bytes", limits::MAX_OUTPUT_BYTES),
        ("max_iterations", limits::MAX_ITERATIONS),
        ("max_context_depth", limits::MAX_CONTEXT_DEPTH),
        ("max_context_nodes", limits::MAX_CONTEXT_NODES),
        ("max_context_bytes", limits::MAX_CONTEXT_BYTES),
        ("max_problems", limits::MAX_PROBLEMS),
        ("max_name_bytes", limits::MAX_NAME_BYTES),
    ]
}

pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyMailTemplate>()?;
    m.add_function(wrap_pyfunction!(describe_template_error, m)?)?;
    m.add_function(wrap_pyfunction!(template_reasons, m)?)?;
    m.add_function(wrap_pyfunction!(template_limits, m)?)?;
    Ok(())
}
