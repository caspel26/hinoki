//! Schema validation and instance construction.
//!
//! Python compiles a schema's closed type model into a table of definitions
//! (see `hinoki.schema`); `CompiledSchema.validate` then walks the decoded JSON
//! once and either returns the finished instance or the ordered list of
//! `(path, code, message)` problems. Semantics, codes and messages follow the
//! pure-Python engine that is kept in `tests/oracle` and compared against this
//! one: strict (no coercion), unknown keys refused, at most 50 problems.

use std::collections::HashSet;

use pyo3::basic::CompareOp;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString};
use regex::Regex;

const MAX_ERRORS: usize = 50;
const MAX_KEY_ECHO: usize = 64;
const MAX_DEPTH: usize = 128;

type Problem = (String, &'static str, String);
type FieldTable<'py> = Vec<(String, Bound<'py, PyAny>, bool)>;

enum Kind {
    Str,
    Int,
    Float,
    Bool,
    Uuid,
    Datetime,
    List(Box<Node>),
    Schema(usize),
}

struct Node {
    kind: Kind,
    nullable: bool,
    min_length: Option<i64>,
    max_length: Option<i64>,
    pattern: Option<(Py<PyAny>, Regex)>,
    ge: Option<Py<PyAny>>,
    le: Option<Py<PyAny>>,
    gt: Option<Py<PyAny>>,
    lt: Option<Py<PyAny>>,
    one_of: Option<Vec<Py<PyAny>>>,
}

struct Field {
    name: String,
    node: Node,
    required: bool,
}

struct Definition {
    class: Py<PyAny>,
    fields: Vec<Field>,
}

/// A schema (and everything reachable from it) ready to validate.
#[pyclass(name = "CompiledSchema", module = "hinoki.native")]
pub struct Compiled {
    definitions: Vec<Definition>,
    root: usize,
    uuid: Py<PyAny>,
    from_iso: Py<PyAny>,
    unset: Py<PyAny>,
}

enum Abort {
    TooMany,
    Depth,
    Python(PyErr),
}

impl From<PyErr> for Abort {
    fn from(err: PyErr) -> Self {
        Abort::Python(err)
    }
}

type Checked<'py> = Result<Option<Bound<'py, PyAny>>, Abort>;

fn compile_error(message: impl Into<String>) -> PyErr {
    pyo3::exceptions::PyValueError::new_err(message.into())
}

fn optional_int(constraints: &Bound<'_, PyDict>, name: &str) -> PyResult<Option<i64>> {
    constraints
        .get_item(name)?
        .map(|v| v.extract::<i64>())
        .transpose()
}

fn optional_object(constraints: &Bound<'_, PyDict>, name: &str) -> PyResult<Option<Py<PyAny>>> {
    Ok(constraints.get_item(name)?.map(Bound::unbind))
}

fn optional_choices(constraints: &Bound<'_, PyDict>) -> PyResult<Option<Vec<Py<PyAny>>>> {
    let Some(values) = constraints.get_item("one_of")? else {
        return Ok(None);
    };
    let mut choices = Vec::new();
    for value in values.try_iter()? {
        choices.push(value?.unbind());
    }
    Ok(Some(choices))
}

fn parse_node(raw: &Bound<'_, PyAny>) -> PyResult<Node> {
    let (kind, nullable, constraints, item, index): (
        String,
        bool,
        Bound<'_, PyDict>,
        Option<Bound<'_, PyAny>>,
        Option<usize>,
    ) = raw.extract()?;
    let kind = match kind.as_str() {
        "str" => Kind::Str,
        "int" => Kind::Int,
        "float" => Kind::Float,
        "bool" => Kind::Bool,
        "uuid" => Kind::Uuid,
        "datetime" => Kind::Datetime,
        "list" => {
            let inner = item.ok_or_else(|| compile_error("list type without an item type"))?;
            Kind::List(Box::new(parse_node(&inner)?))
        }
        "schema" => {
            Kind::Schema(index.ok_or_else(|| compile_error("schema type without a schema"))?)
        }
        other => return Err(compile_error(format!("unknown field kind {other:?}"))),
    };
    let pattern = match constraints.get_item("pattern")? {
        Some(text) => {
            let source: String = text.extract()?;
            let regex = Regex::new(&format!("\\A(?:{source})\\z"))
                .map_err(|e| compile_error(format!("unsupported pattern {source:?}: {e}")))?;
            Some((text.unbind(), regex))
        }
        None => None,
    };
    Ok(Node {
        kind,
        nullable,
        min_length: optional_int(&constraints, "min_length")?,
        max_length: optional_int(&constraints, "max_length")?,
        pattern,
        ge: optional_object(&constraints, "ge")?,
        le: optional_object(&constraints, "le")?,
        gt: optional_object(&constraints, "gt")?,
        lt: optional_object(&constraints, "lt")?,
        one_of: optional_choices(&constraints)?,
    })
}

/// Build a validator from `definitions`: a list of `(class, [(name, node,
/// required), ...])`; nested schemas refer to a definition by index.
#[pyfunction]
pub fn compile_schemas(
    definitions: Vec<(Py<PyAny>, FieldTable<'_>)>,
    root: usize,
    uuid_class: Py<PyAny>,
    from_iso: Py<PyAny>,
    unset: Py<PyAny>,
) -> PyResult<Compiled> {
    let mut built = Vec::with_capacity(definitions.len());
    for (class, fields) in definitions {
        let mut parsed = Vec::with_capacity(fields.len());
        for (name, node, required) in fields {
            parsed.push(Field {
                name,
                node: parse_node(&node)?,
                required,
            });
        }
        built.push(Definition {
            class,
            fields: parsed,
        });
    }
    if root >= built.len() {
        return Err(compile_error("root schema is not in the table"));
    }
    let known = built.len();
    for definition in &built {
        for field in &definition.fields {
            if refers_outside(&field.node, known) {
                return Err(compile_error("a schema refers to a missing definition"));
            }
        }
    }
    Ok(Compiled {
        definitions: built,
        root,
        uuid: uuid_class,
        from_iso,
        unset,
    })
}

/// For a `Schema` instance: its field names in declaration order and the
/// `UNSET` sentinel (fields holding it are not serialised). The class is
/// compiled on first use. `None` when `value` is not a schema instance (or,
/// with `honour_custom_dump`, when its class overrides `dump()`).
pub fn instance_fields(
    value: &Bound<'_, PyAny>,
    honour_custom_dump: bool,
) -> PyResult<Option<(Vec<String>, Py<PyAny>)>> {
    let class = value.get_type();
    if !class.hasattr("__hinoki_schema__")? {
        return Ok(None);
    }
    // Read the class's own entries: a subclass must not use its parent's table.
    let table = class.getattr("__dict__")?;
    // A class that overrides `dump()` decides what it exposes: the caller falls
    // back to calling it, exactly as the Python encoder always did.
    if honour_custom_dump {
        let overrides = match table.get_item("__native_dump__") {
            Ok(flag) => !flag.is_truthy()?,
            Err(_) => {
                class.call_method0("native_schema")?;
                !class
                    .getattr("__dict__")?
                    .get_item("__native_dump__")?
                    .is_truthy()?
            }
        };
        if overrides {
            return Ok(None);
        }
    }
    let own = table.get_item("__native__").ok();
    let compiled = match own {
        Some(found) if !found.is_none() => found,
        _ => class.call_method0("native_schema")?,
    };
    let compiled: PyRef<'_, Compiled> = compiled.extract()?;
    let names = compiled.definitions[compiled.root]
        .fields
        .iter()
        .map(|field| field.name.clone())
        .collect();
    Ok(Some((names, compiled.unset.clone_ref(value.py()))))
}

/// Compile-check a `pattern=` at registration: the Rust `regex` syntax (linear
/// time, no look-around or back-references) matched against the whole value.
#[pyfunction]
pub fn check_pattern(source: &str) -> PyResult<()> {
    let verbose =
        Regex::new(r"\(\?[a-zA-Z]*x[a-zA-Z-]*[:)]").map_err(|e| compile_error(e.to_string()))?;
    if verbose.is_match(source) {
        return Err(compile_error("verbose mode (?x) is not supported"));
    }
    for banned in ["&&", "~~", "[[:"] {
        if source.contains(banned) {
            return Err(compile_error(format!(
                "{banned:?} (character-class set operations and POSIX classes) is not supported"
            )));
        }
    }
    Regex::new(&format!("\\A(?:{source})\\z"))
        .map(|_| ())
        .map_err(|e| compile_error(e.to_string()))
}

fn refers_outside(node: &Node, known: usize) -> bool {
    match &node.kind {
        Kind::Schema(index) => *index >= known,
        Kind::List(inner) => refers_outside(inner, known),
        _ => false,
    }
}

fn join(path: &str, name: &str) -> String {
    if path.is_empty() {
        name.to_owned()
    } else {
        format!("{path}.{name}")
    }
}

struct Run<'a, 'py> {
    py: Python<'py>,
    compiled: &'a Compiled,
    problems: Vec<Problem>,
}

impl<'py> Run<'_, 'py> {
    fn fail(&mut self, path: &str, code: &'static str, message: String) -> Checked<'py> {
        self.problems.push((path.to_owned(), code, message));
        if self.problems.len() >= MAX_ERRORS {
            return Err(Abort::TooMany);
        }
        Ok(None)
    }

    fn check(
        &mut self,
        node: &Node,
        data: &Bound<'py, PyAny>,
        path: &str,
        depth: usize,
    ) -> Checked<'py> {
        if depth > MAX_DEPTH {
            return Err(Abort::Depth);
        }
        if data.is_none() {
            if node.nullable {
                return Ok(Some(data.clone()));
            }
            return self.fail(path, "null", "null is not allowed".to_owned());
        }
        let checked = match &node.kind {
            Kind::Str => self.text(node, data, path)?,
            Kind::Int => self.integer(node, data, path)?,
            Kind::Float => self.number(node, data, path)?,
            Kind::Bool => self.boolean(data, path)?,
            Kind::Uuid => self.uuid(data, path)?,
            Kind::Datetime => self.moment(data, path)?,
            Kind::List(item) => self.sequence(node, item, data, path, depth)?,
            Kind::Schema(index) => self.schema(*index, data, path, depth)?,
        };
        match (&node.one_of, checked) {
            (Some(choices), Some(value)) => self.one_of(choices, value, path),
            (_, other) => Ok(other),
        }
    }

    /// The value must equal one of the declared choices (strict types were
    /// already enforced by the kind, so `1` never matches `True`).
    fn one_of(
        &mut self,
        choices: &[Py<PyAny>],
        value: Bound<'py, PyAny>,
        path: &str,
    ) -> Checked<'py> {
        for choice in choices {
            if value.eq(choice.bind(self.py))? {
                return Ok(Some(value));
            }
        }
        let shown: Vec<String> = choices
            .iter()
            .take(10)
            .map(|c| {
                c.bind(self.py)
                    .repr()
                    .map(|r| r.to_string())
                    .unwrap_or_default()
            })
            .collect();
        self.fail(
            path,
            "one_of",
            format!("must be one of {}", shown.join(", ")),
        )
    }

    fn length(&mut self, node: &Node, size: usize, path: &str) -> Result<bool, Abort> {
        let size = i64::try_from(size).unwrap_or(i64::MAX);
        if let Some(least) = node.min_length {
            if size < least {
                self.fail(path, "min_length", format!("at least {least} long"))?;
                return Ok(false);
            }
        }
        if let Some(most) = node.max_length {
            if size > most {
                self.fail(path, "max_length", format!("at most {most} long"))?;
                return Ok(false);
            }
        }
        Ok(true)
    }

    fn text(&mut self, node: &Node, data: &Bound<'py, PyAny>, path: &str) -> Checked<'py> {
        let Ok(text) = data.cast::<PyString>() else {
            return self.fail(path, "type", "expected a string".to_owned());
        };
        if !self.length(node, text.len()?, path)? {
            return Ok(None);
        }
        if let Some((source, regex)) = &node.pattern {
            let matched = text.to_cow().map(|s| regex.is_match(&s)).unwrap_or(false);
            if !matched {
                let shown = source.bind(self.py).repr()?;
                return self.fail(path, "pattern", format!("must match {shown}"));
            }
        }
        Ok(Some(data.clone()))
    }

    fn bounds(&mut self, node: &Node, data: &Bound<'py, PyAny>, path: &str) -> Checked<'py> {
        let tests = [
            ("ge", "greater than or equal to", CompareOp::Ge, &node.ge),
            ("le", "less than or equal to", CompareOp::Le, &node.le),
            ("gt", "greater than", CompareOp::Gt, &node.gt),
            ("lt", "less than", CompareOp::Lt, &node.lt),
        ];
        for (name, words, op, limit) in tests {
            if let Some(limit) = limit {
                let limit = limit.bind(self.py);
                if !data.rich_compare(limit, op)?.is_truthy()? {
                    return self.fail(path, name, format!("must be {words} {}", limit.str()?));
                }
            }
        }
        Ok(Some(data.clone()))
    }

    fn integer(&mut self, node: &Node, data: &Bound<'py, PyAny>, path: &str) -> Checked<'py> {
        if !data.is_exact_instance_of::<PyInt>() {
            return self.fail(path, "type", "expected an integer".to_owned());
        }
        self.bounds(node, data, path)
    }

    fn number(&mut self, node: &Node, data: &Bound<'py, PyAny>, path: &str) -> Checked<'py> {
        if !(data.is_exact_instance_of::<PyInt>() || data.is_exact_instance_of::<PyFloat>()) {
            return self.fail(path, "type", "expected a number".to_owned());
        }
        let Ok(value) = data.extract::<f64>() else {
            return self.fail(path, "finite", "number is out of range".to_owned());
        };
        if !value.is_finite() {
            return self.fail(path, "finite", "number must be finite".to_owned());
        }
        let converted = PyFloat::new(self.py, value).into_any();
        self.bounds(node, &converted, path)
    }

    fn boolean(&mut self, data: &Bound<'py, PyAny>, path: &str) -> Checked<'py> {
        if !data.is_exact_instance_of::<PyBool>() {
            return self.fail(path, "type", "expected a boolean".to_owned());
        }
        Ok(Some(data.clone()))
    }

    fn uuid(&mut self, data: &Bound<'py, PyAny>, path: &str) -> Checked<'py> {
        let canonical = data
            .cast::<PyString>()
            .ok()
            .and_then(|s| s.to_cow().ok().map(|c| is_canonical_uuid(&c)))
            .unwrap_or(false);
        if !canonical {
            return self.fail(path, "uuid", "expected a canonical UUID string".to_owned());
        }
        Ok(Some(self.compiled.uuid.bind(self.py).call1((data,))?))
    }

    fn moment(&mut self, data: &Bound<'py, PyAny>, path: &str) -> Checked<'py> {
        let shaped = data
            .cast::<PyString>()
            .ok()
            .and_then(|s| s.to_cow().ok().map(|c| is_rfc3339(&c)))
            .unwrap_or(false);
        let message = "expected an RFC 3339 datetime string";
        if !shaped {
            return self.fail(path, "datetime", message.to_owned());
        }
        let parsed = match self.compiled.from_iso.bind(self.py).call1((data,)) {
            Ok(value) => value,
            Err(err) if err.is_instance_of::<pyo3::exceptions::PyValueError>(self.py) => {
                return self.fail(path, "datetime", message.to_owned());
            }
            Err(err) => return Err(err.into()),
        };
        if parsed.getattr("tzinfo")?.is_none() {
            return self.fail(
                path,
                "datetime_tz",
                "datetime needs a UTC offset".to_owned(),
            );
        }
        Ok(Some(parsed))
    }

    fn sequence(
        &mut self,
        node: &Node,
        item: &Node,
        data: &Bound<'py, PyAny>,
        path: &str,
        depth: usize,
    ) -> Checked<'py> {
        let Ok(list) = data.cast::<PyList>() else {
            return self.fail(path, "type", "expected an array".to_owned());
        };
        if !self.length(node, list.len(), path)? {
            return Ok(None);
        }
        let before = self.problems.len();
        let out = PyList::empty(self.py);
        for (index, element) in list.iter().enumerate() {
            let found = self.check(item, &element, &format!("{path}[{index}]"), depth + 1)?;
            if let Some(value) = found {
                out.append(value)?;
            }
        }
        Ok(if self.problems.len() > before {
            None
        } else {
            Some(out.into_any())
        })
    }

    fn schema(
        &mut self,
        index: usize,
        data: &Bound<'py, PyAny>,
        path: &str,
        depth: usize,
    ) -> Checked<'py> {
        let Ok(object) = data.cast::<PyDict>() else {
            return self.fail(path, "type", "expected an object".to_owned());
        };
        let compiled = self.compiled;
        let definition = &compiled.definitions[index];
        let before = self.problems.len();
        let known: HashSet<&str> = definition.fields.iter().map(|f| f.name.as_str()).collect();
        for key in object.keys() {
            let name = key.str()?.to_string_lossy().into_owned();
            let is_field = key
                .cast::<PyString>()
                .ok()
                .and_then(|k| k.to_cow().ok())
                .is_some_and(|k| known.contains(&*k));
            if !is_field {
                let shown: String = name.chars().take(MAX_KEY_ECHO).collect();
                self.fail(
                    &join(path, &shown),
                    "extra_forbidden",
                    "unknown field".to_owned(),
                )?;
            }
        }
        let values = PyDict::new(self.py);
        for field in &definition.fields {
            let here = join(path, &field.name);
            match object.get_item(&field.name)? {
                Some(raw) => {
                    if let Some(value) = self.check(&field.node, &raw, &here, depth + 1)? {
                        values.set_item(&field.name, value)?;
                    }
                }
                None if field.required => {
                    self.fail(&here, "required", "field is required".to_owned())?;
                }
                None => {}
            }
        }
        if self.problems.len() > before {
            return Ok(None);
        }
        Ok(Some(
            definition.class.bind(self.py).call((), Some(&values))?,
        ))
    }
}

fn is_canonical_uuid(text: &str) -> bool {
    let bytes = text.as_bytes();
    bytes.len() == 36
        && bytes.iter().enumerate().all(|(at, b)| match at {
            8 | 13 | 18 | 23 => *b == b'-',
            _ => b.is_ascii_hexdigit(),
        })
}

/// `YYYY-MM-DDThh:mm:ss[.fraction][Z|+hh:mm]` (the offset is optional here; a
/// missing one is reported separately as `datetime_tz`).
fn is_rfc3339(text: &str) -> bool {
    let bytes = text.as_bytes();
    let digits = |from: usize, count: usize| {
        bytes.len() >= from + count && bytes[from..from + count].iter().all(u8::is_ascii_digit)
    };
    let shape = digits(0, 4)
        && bytes.get(4) == Some(&b'-')
        && digits(5, 2)
        && bytes.get(7) == Some(&b'-')
        && digits(8, 2)
        && matches!(bytes.get(10), Some(b'T' | b't'))
        && digits(11, 2)
        && bytes.get(13) == Some(&b':')
        && digits(14, 2)
        && bytes.get(16) == Some(&b':')
        && digits(17, 2);
    if !shape {
        return false;
    }
    let mut at = 19;
    if bytes.get(at) == Some(&b'.') {
        let start = at + 1;
        at = start;
        while bytes.get(at).is_some_and(u8::is_ascii_digit) {
            at += 1;
        }
        if at == start {
            return false;
        }
    }
    match &bytes[at..] {
        [] | [b'Z' | b'z'] => true,
        [b'+' | b'-', ..] => {
            let rest = &bytes[at + 1..];
            rest.len() == 5
                && rest[..2].iter().all(u8::is_ascii_digit)
                && rest[2] == b':'
                && rest[3..].iter().all(u8::is_ascii_digit)
        }
        _ => false,
    }
}

#[pymethods]
impl Compiled {
    /// `(instance, problems)`: exactly one of them is meaningful. Problems are
    /// `(path, code, message)` tuples in the order the checks found them.
    fn validate<'py>(
        &self,
        py: Python<'py>,
        data: &Bound<'py, PyAny>,
    ) -> PyResult<(Option<Bound<'py, PyAny>>, Vec<Problem>)> {
        let mut run = Run {
            py,
            compiled: self,
            problems: Vec::new(),
        };
        match run.schema(self.root, data, "", 0) {
            Ok(value) => Ok((value, run.problems)),
            Err(Abort::TooMany) => {
                let mut problems = run.problems;
                problems.push((
                    String::new(),
                    "too_many_errors",
                    "further errors omitted".to_owned(),
                ));
                Ok((None, problems))
            }
            Err(Abort::Depth) => Ok((
                None,
                vec![(
                    String::new(),
                    "depth",
                    "input is nested too deeply".to_owned(),
                )],
            )),
            Err(Abort::Python(err)) => Err(err),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn canonical_uuids() {
        assert!(is_canonical_uuid("12345678-1234-5678-1234-567812345678"));
        assert!(is_canonical_uuid("ABCDEF00-1234-5678-1234-567812345678"));
        assert!(!is_canonical_uuid("12345678123456781234567812345678"));
        assert!(!is_canonical_uuid("{12345678-1234-5678-1234-567812345678}"));
        assert!(!is_canonical_uuid("1234567g-1234-5678-1234-567812345678"));
    }

    #[test]
    fn rfc3339_shapes() {
        for good in [
            "2026-11-13T10:00:00",
            "2026-11-13t10:00:00Z",
            "2026-11-13T10:00:00.123+01:00",
            "2026-11-13T10:00:00-05:30",
        ] {
            assert!(is_rfc3339(good), "{good}");
        }
        for bad in [
            "2026-11-13",
            "2026-11-13 10:00:00",
            "2026-11-13T10:00:00.",
            "2026-11-13T10:00:00+0100",
            "2026-11-13T10:00:00Zx",
            "２０２６-11-13T10:00:00",
        ] {
            assert!(!is_rfc3339(bad), "{bad}");
        }
    }
}

const DUMP_DEPTH: usize = 1000;

fn exact_class(value: &Bound<'_, PyAny>, module: &str, name: &str) -> PyResult<bool> {
    let wanted = value.py().import(module)?.getattr(name)?;
    Ok(value.get_type().is(&wanted))
}

/// A value as `Schema.dump()` (JSON-compatible) or `Schema.to_dict()` (Python
/// values) shows it: nested schemas become dicts, lists and tuples lists, dicts
/// are copied; in JSON mode exact `UUID`/`datetime` values become text and any
/// object with a `dump()` method is replaced by what it returns.
fn convert<'py>(
    value: &Bound<'py, PyAny>,
    json_mode: bool,
    depth: usize,
) -> PyResult<Bound<'py, PyAny>> {
    let py = value.py();
    if depth > DUMP_DEPTH {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "value is nested too deeply",
        ));
    }
    if let Ok(list) = value.cast::<PyList>() {
        let out = PyList::empty(py);
        for item in list.iter() {
            out.append(convert(&item, json_mode, depth + 1)?)?;
        }
        return Ok(out.into_any());
    }
    if let Ok(tuple) = value.cast::<pyo3::types::PyTuple>() {
        let out = PyList::empty(py);
        for item in tuple.iter() {
            out.append(convert(&item, json_mode, depth + 1)?)?;
        }
        return Ok(out.into_any());
    }
    if let Ok(dict) = value.cast::<PyDict>() {
        let out = PyDict::new(py);
        for (key, item) in dict.iter() {
            out.set_item(key, convert(&item, json_mode, depth + 1)?)?;
        }
        return Ok(out.into_any());
    }
    if instance_fields(value, false)?.is_some() {
        if json_mode && instance_fields(value, true)?.is_none() {
            return value.call_method0("dump"); // the class decides what it exposes
        }
        return Ok(dump_instance_at(value, json_mode, &[], depth + 1)?.into_any());
    }
    if json_mode {
        if exact_class(value, "uuid", "UUID")? {
            return Ok(value.str()?.into_any());
        }
        if exact_class(value, "datetime", "datetime")? {
            return value.call_method0("isoformat");
        }
        if value.hasattr("dump")? {
            return value.call_method0("dump");
        }
    }
    Ok(value.clone())
}

fn dump_instance_at<'py>(
    value: &Bound<'py, PyAny>,
    json_mode: bool,
    exclude: &[String],
    depth: usize,
) -> PyResult<Bound<'py, PyDict>> {
    let py = value.py();
    let Some((names, unset)) = instance_fields(value, false)? else {
        return Err(pyo3::exceptions::PyTypeError::new_err(
            "not a schema instance",
        ));
    };
    let out = PyDict::new(py);
    for name in names {
        if exclude.contains(&name) {
            continue;
        }
        let item = value.getattr(name.as_str())?;
        if item.is(&unset) {
            continue;
        }
        out.set_item(&name, convert(&item, json_mode, depth)?)?;
    }
    Ok(out)
}

/// The fields of a schema instance as a dict (UNSET omitted): JSON-compatible
/// when `json_mode`, Python values otherwise. `exclude` names fields to skip.
#[pyfunction]
#[pyo3(signature = (value, json_mode, exclude=Vec::new()))]
pub fn dump_instance<'py>(
    value: &Bound<'py, PyAny>,
    json_mode: bool,
    exclude: Vec<String>,
) -> PyResult<Bound<'py, PyDict>> {
    dump_instance_at(value, json_mode, &exclude, 0)
}

/// How one model attribute converts for a schema field: a nested model becomes
/// `schema.from_model(value)`, a list or tuple maps its items through `item`,
/// anything else passes through unchanged.
struct Conversion {
    schema: Option<Py<PyAny>>,
    item: Option<Box<Conversion>>,
}

fn parse_conversion(raw: &Bound<'_, PyAny>) -> PyResult<Conversion> {
    let (schema, item): (Option<Py<PyAny>>, Option<Bound<'_, PyAny>>) = raw.extract()?;
    Ok(Conversion {
        schema,
        item: match item {
            Some(inner) => Some(Box::new(parse_conversion(&inner)?)),
            None => None,
        },
    })
}

struct PlanField {
    key: Py<pyo3::types::PyString>,
    name: String,
    required: bool,
    conversion: Conversion,
}

/// The fields of a schema as `from_model` reads them, prepared once per class.
#[pyclass(name = "ModelPlan", module = "hinoki.native")]
pub struct ModelPlan {
    class: Py<PyAny>,
    fields: Vec<PlanField>,
}

#[pymethods]
impl ModelPlan {
    /// `fields` is `[(name, required, (schema, item)), ...]`.
    #[new]
    fn new(class: Py<PyAny>, fields: Vec<(String, bool, Bound<'_, PyAny>)>) -> PyResult<Self> {
        let mut planned = Vec::with_capacity(fields.len());
        for (name, required, conversion) in fields {
            planned.push(PlanField {
                key: pyo3::types::PyString::new(conversion.py(), &name).unbind(),
                name,
                required,
                conversion: parse_conversion(&conversion)?,
            });
        }
        Ok(Self {
            class,
            fields: planned,
        })
    }

    /// `Schema.from_model` in Rust: read each planned field from `row` (`extra`
    /// wins), convert nested models and build the instance. An unloaded relation
    /// still raises `RelationNotLoaded`; a missing required attribute raises
    /// `AttributeError`.
    #[pyo3(signature = (row, extra=None))]
    fn build<'py>(
        &self,
        py: Python<'py>,
        row: &Bound<'py, PyAny>,
        extra: Option<&Bound<'py, PyDict>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let class = self.class.bind(py);
        let values = PyDict::new(py);
        for field in &self.fields {
            let key = field.key.bind(py);
            if let Some(given) = extra {
                if let Some(found) = given.get_item(key)? {
                    values.set_item(key, found)?;
                    continue;
                }
            }
            match row.getattr(key) {
                Ok(value) => values.set_item(key, convert_attribute(&value, &field.conversion)?)?,
                Err(error) => self.missing(py, row, field, error)?,
            }
        }
        if values.len() == self.fields.len() {
            // Every field is present: build the instance directly, skipping the
            // dataclass constructor (the dominant cost of constructing a schema).
            let object_new = py
                .import("builtins")?
                .getattr("object")?
                .getattr("__new__")?;
            let instance = object_new.call1((class,))?;
            instance
                .getattr("__dict__")?
                .cast::<PyDict>()?
                .update(values.as_mapping())?;
            return Ok(instance);
        }
        class.call((), Some(&values))
    }
}

impl ModelPlan {
    /// An attribute was not there: propagate a loading mistake or a real error,
    /// raise for a required field, skip an optional one.
    fn missing(
        &self,
        py: Python<'_>,
        row: &Bound<'_, PyAny>,
        field: &PlanField,
        error: PyErr,
    ) -> PyResult<()> {
        let unloaded = py.import("hinoki.errors")?.getattr("RelationNotLoaded")?;
        if error.value(py).is_instance(&unloaded)?
            || !error.is_instance_of::<pyo3::exceptions::PyAttributeError>(py)
        {
            return Err(error); // a loading mistake or an unrelated failure
        }
        if field.required {
            let schema_name: String = self.class.bind(py).getattr("__name__")?.extract()?;
            let row_name: String = row.get_type().name()?.extract()?;
            let name = &field.name;
            return Err(pyo3::exceptions::PyAttributeError::new_err(format!(
                "{schema_name}.{name}: {row_name} has no attribute '{name}'; pass it as {name}=..."
            )));
        }
        Ok(())
    }
}

fn convert_attribute<'py>(
    value: &Bound<'py, PyAny>,
    conversion: &Conversion,
) -> PyResult<Bound<'py, PyAny>> {
    if value.is_none() {
        return Ok(value.clone());
    }
    if let Some(class) = &conversion.schema {
        if value.get_type().hasattr("__table__")? {
            return class.bind(value.py()).call_method1("from_model", (value,));
        }
    }
    if let Some(inner) = &conversion.item {
        if value.is_instance_of::<PyList>() || value.is_instance_of::<pyo3::types::PyTuple>() {
            let out = PyList::empty(value.py());
            for element in value.try_iter()? {
                out.append(convert_attribute(&element?, inner)?)?;
            }
            return Ok(out.into_any());
        }
    }
    Ok(value.clone())
}
