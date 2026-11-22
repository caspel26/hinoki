//! Typed list filters: a client's query string checked against a declared
//! allow-list and turned into typed conditions.
//!
//! Only names the declaration lists are accepted, and each one only in the
//! operator the declaration gave it. Values are coerced to the declared type
//! here and never reach the SQL text: the conditions carry typed values that
//! the query compiler binds as parameters, and every column name comes from
//! the declaration, never from the request.

use std::collections::HashMap;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList, PyTuple};
use time::format_description::well_known::Rfc3339;
use time::{Date, Month, OffsetDateTime, UtcOffset};
use uuid::Uuid;

const MAX_NUMBER_DIGITS: usize = 18;
const MAX_DECIMAL_DIGITS: usize = 38;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Kind {
    Bool,
    Int,
    Float,
    Str,
    Uuid,
    Date,
    DateTime,
    Decimal,
}

impl Kind {
    pub(crate) fn parse(name: &str) -> Option<Self> {
        Some(match name {
            "bool" => Self::Bool,
            "int" => Self::Int,
            "float" => Self::Float,
            "str" => Self::Str,
            "uuid" => Self::Uuid,
            "date" => Self::Date,
            "datetime" => Self::DateTime,
            "decimal" => Self::Decimal,
            _ => return None,
        })
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Shape {
    Eq,
    In,
    Range,
}

impl Shape {
    pub(crate) fn parse(name: &str) -> Option<Self> {
        Some(match name {
            "eq" => Self::Eq,
            "in" => Self::In,
            "range" => Self::Range,
            _ => return None,
        })
    }
}

/// A value coerced to the declared type.
#[derive(Clone, Debug, PartialEq)]
pub(crate) enum Typed {
    Bool(bool),
    Int(i64),
    Float(f64),
    Text(String),
    Uuid(Uuid),
    Date(Date),
    Moment(OffsetDateTime),
    Decimal(String),
}

impl Typed {
    /// A stable text form, used to bind a cursor to the filters it was issued for.
    fn canonical(&self) -> String {
        match self {
            Self::Bool(value) => value.to_string(),
            Self::Int(value) => value.to_string(),
            Self::Float(value) => format!("{value:?}"),
            Self::Text(value) | Self::Decimal(value) => value.clone(),
            Self::Uuid(value) => value.hyphenated().to_string(),
            Self::Date(value) => value.to_string(),
            Self::Moment(value) => value.unix_timestamp_nanos().to_string(),
        }
    }
}

#[derive(Clone, Debug)]
pub(crate) struct Field {
    pub(crate) name: String,
    pub(crate) kind: Kind,
    pub(crate) shape: Shape,
    pub(crate) nullable: bool,
    pub(crate) choices: Vec<String>,
}

#[derive(Clone, Copy, Debug)]
pub(crate) struct Limits {
    pub(crate) max_in: usize,
    pub(crate) max_text: usize,
    pub(crate) max_search: usize,
}

/// One rejected parameter: where, a short code and a message that never
/// repeats the value.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Problem {
    pub(crate) path: String,
    pub(crate) code: &'static str,
    pub(crate) message: String,
}

impl Problem {
    fn new(path: &str, code: &'static str, message: impl Into<String>) -> Self {
        Self {
            path: path.chars().take(64).collect(),
            code,
            message: message.into(),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Role {
    Plain(usize),
    From(usize),
    To(usize),
    Null(usize),
    Search,
    Ordering,
}

/// What one declared field received from the request.
#[derive(Clone, Debug, Default)]
pub(crate) struct Entry {
    pub(crate) field: usize,
    pub(crate) eq: Option<Typed>,
    pub(crate) many: Option<Vec<Typed>>,
    pub(crate) from: Option<Typed>,
    pub(crate) to: Option<Typed>,
    pub(crate) null: Option<bool>,
}

/// A checked request: typed values by field, the search term and the
/// ordering, each as the declaration allows.
#[derive(Clone, Debug, Default)]
pub(crate) struct Parsed {
    pub(crate) entries: Vec<Entry>,
    pub(crate) term: Option<String>,
    pub(crate) order: Option<Vec<String>>,
}

#[derive(Debug)]
pub(crate) struct Spec {
    pub(crate) fields: Vec<Field>,
    pub(crate) search: Vec<String>,
    pub(crate) ordering: Vec<String>,
    pub(crate) limits: Limits,
    roles: HashMap<String, Role>,
}

pub(crate) type Query<'a> = &'a [(String, Vec<String>)];

fn repeated(names: &[String]) -> Option<&String> {
    names
        .iter()
        .enumerate()
        .find_map(|(index, name)| names[..index].contains(name).then_some(name))
}

impl Spec {
    pub(crate) fn new(
        fields: Vec<Field>,
        search: Vec<String>,
        ordering: Vec<String>,
        limits: Limits,
    ) -> Result<Self, String> {
        let mut roles = HashMap::new();
        for (index, field) in fields.iter().enumerate() {
            for (name, role) in field_roles(field, index) {
                if roles.insert(name.clone(), role).is_some() {
                    return Err(format!("the parameter {name:?} is declared twice"));
                }
            }
        }
        for (name, present, role) in [
            ("search", !search.is_empty(), Role::Search),
            ("ordering", !ordering.is_empty(), Role::Ordering),
        ] {
            if present && roles.insert(name.to_owned(), role).is_some() {
                return Err(format!("the parameter {name:?} is declared twice"));
            }
        }
        if let Some(name) = repeated(&ordering) {
            return Err(format!("ordering names {name:?} twice"));
        }
        Ok(Self {
            fields,
            search,
            ordering,
            limits,
            roles,
        })
    }

    /// Check `query`, ignoring the names in `reserved` (the pagination
    /// parameters). Every problem is reported, none is skipped.
    pub(crate) fn parse(
        &self,
        query: Query<'_>,
        reserved: &[String],
    ) -> Result<Parsed, Vec<Problem>> {
        let mut parsed = Parsed::default();
        let mut found: Vec<Option<Entry>> = vec![None; self.fields.len()];
        let mut problems = Vec::new();
        for (name, values) in query {
            if reserved.contains(name) {
                continue;
            }
            if let Err(problem) = self.take(name, values, &mut found, &mut parsed) {
                problems.push(problem);
            }
        }
        if !problems.is_empty() {
            return Err(problems);
        }
        parsed.entries = found.into_iter().flatten().collect();
        Ok(parsed)
    }

    fn take(
        &self,
        name: &str,
        values: &[String],
        found: &mut [Option<Entry>],
        parsed: &mut Parsed,
    ) -> Result<(), Problem> {
        let Some(role) = self.roles.get(name).copied() else {
            return Err(self.unknown(name));
        };
        let [raw] = values else {
            return Err(Problem::new(name, "duplicate", "given more than once"));
        };
        if raw.is_empty() {
            return Ok(());
        }
        match role {
            Role::Search => parsed.term = Some(self.search_term(name, raw)?),
            Role::Ordering => parsed.order = Some(self.order(name, raw)?),
            Role::Plain(index) | Role::From(index) | Role::To(index) | Role::Null(index) => {
                let entry = found[index].get_or_insert_with(|| Entry {
                    field: index,
                    ..Entry::default()
                });
                self.fill(name, role, raw, entry)?;
            }
        }
        Ok(())
    }

    fn unknown(&self, name: &str) -> Problem {
        let base = name.split('.').next().unwrap_or(name);
        if self.fields.iter().any(|field| field.name == base) {
            Problem::new(
                name,
                "operator",
                "this operator is not allowed for this field",
            )
        } else {
            Problem::new(name, "unknown", "unknown filter")
        }
    }

    fn fill(&self, name: &str, role: Role, raw: &str, entry: &mut Entry) -> Result<(), Problem> {
        let field = &self.fields[entry.field];
        match role {
            Role::Plain(_) if field.shape == Shape::In => {
                entry.many = Some(self.list(name, field, raw)?);
            }
            Role::Plain(_) => entry.eq = Some(self.value(name, field, raw)?),
            Role::From(_) => entry.from = Some(self.value(name, field, raw)?),
            Role::To(_) => entry.to = Some(self.value(name, field, raw)?),
            Role::Null(_) => entry.null = Some(boolean(name, raw)?),
            Role::Search | Role::Ordering => {}
        }
        Ok(())
    }

    fn list(&self, name: &str, field: &Field, raw: &str) -> Result<Vec<Typed>, Problem> {
        let items: Vec<&str> = raw.split(',').collect();
        if items.len() > self.limits.max_in {
            return Err(Problem::new(
                name,
                "too_many",
                format!("at most {} values", self.limits.max_in),
            ));
        }
        items
            .into_iter()
            .map(|item| self.value(name, field, item))
            .collect()
    }

    fn value(&self, name: &str, field: &Field, raw: &str) -> Result<Typed, Problem> {
        if raw.is_empty() {
            return Err(Problem::new(name, "type", "empty value"));
        }
        match field.kind {
            Kind::Bool => boolean(name, raw).map(Typed::Bool),
            Kind::Int => integer(name, raw),
            Kind::Float => float(name, raw),
            Kind::Str => self.text(name, field, raw),
            Kind::Uuid => uuid(name, raw),
            Kind::Date => date(name, raw),
            Kind::DateTime => moment(name, raw),
            Kind::Decimal => decimal(name, raw),
        }
    }

    fn text(&self, name: &str, field: &Field, raw: &str) -> Result<Typed, Problem> {
        plain_text(name, raw, self.limits.max_text)?;
        if !field.choices.is_empty() && !field.choices.iter().any(|choice| choice == raw) {
            let allowed = field.choices.join(", ");
            return Err(Problem::new(
                name,
                "choice",
                format!("must be one of: {allowed}"),
            ));
        }
        Ok(Typed::Text(raw.to_owned()))
    }

    fn search_term(&self, name: &str, raw: &str) -> Result<String, Problem> {
        plain_text(name, raw, self.limits.max_search)?;
        Ok(raw.to_owned())
    }

    fn order(&self, name: &str, raw: &str) -> Result<Vec<String>, Problem> {
        let mut order: Vec<String> = Vec::new();
        for item in raw.split(',') {
            let column = item.strip_prefix('-').unwrap_or(item);
            if !self.ordering.iter().any(|allowed| allowed == column) {
                let allowed = self.ordering.join(", ");
                return Err(Problem::new(
                    name,
                    "choice",
                    format!("order by one of: {allowed}, each optionally prefixed with -"),
                ));
            }
            if order
                .iter()
                .any(|seen| seen.trim_start_matches('-') == column)
            {
                return Err(Problem::new(name, "duplicate", "a field is ordered twice"));
            }
            order.push(item.to_owned());
        }
        Ok(order)
    }

    /// The conditions as `(column, operator, values)`, in field order, then
    /// the search. Operators are those of the query compiler.
    pub(crate) fn conditions(&self, parsed: &Parsed) -> Vec<(String, &'static str, Vec<Typed>)> {
        let mut found = Vec::new();
        for entry in &parsed.entries {
            let column = &self.fields[entry.field].name;
            push_entry(&mut found, column, entry);
        }
        if let Some(term) = &parsed.term {
            let operator = if self.search.len() > 1 {
                "containsany"
            } else {
                "contains"
            };
            found.push((
                self.search.join(","),
                operator,
                vec![Typed::Text(term.clone())],
            ));
        }
        found
    }

    /// What a cursor must be bound to so that it only serves the filters it
    /// was issued for. Empty without filters, so unfiltered cursors are unchanged.
    pub(crate) fn scope(&self, parsed: &Parsed) -> Vec<u8> {
        let mut parts: Vec<(String, String)> = Vec::new();
        for entry in &parsed.entries {
            let name = &self.fields[entry.field].name;
            scope_entry(&mut parts, name, entry);
        }
        if let Some(term) = &parsed.term {
            parts.push(("search".to_owned(), term.clone()));
        }
        if parts.is_empty() {
            return Vec::new();
        }
        let mut out = b"filters\x01".to_vec();
        for (key, value) in parts {
            for text in [key, value] {
                out.extend_from_slice(&(text.len() as u32).to_be_bytes());
                out.extend_from_slice(text.as_bytes());
            }
        }
        out
    }
}

fn field_roles(field: &Field, index: usize) -> Vec<(String, Role)> {
    let mut roles = match field.shape {
        Shape::Range => vec![
            (format!("{}.from", field.name), Role::From(index)),
            (format!("{}.to", field.name), Role::To(index)),
        ],
        Shape::Eq | Shape::In => vec![(field.name.clone(), Role::Plain(index))],
    };
    if field.nullable {
        roles.push((format!("{}.null", field.name), Role::Null(index)));
    }
    roles
}

fn push_entry(found: &mut Vec<(String, &'static str, Vec<Typed>)>, column: &str, entry: &Entry) {
    if let Some(value) = &entry.eq {
        found.push((column.to_owned(), "eq", vec![value.clone()]));
    }
    if let Some(values) = &entry.many {
        found.push((column.to_owned(), "in", values.clone()));
    }
    if let Some(value) = &entry.from {
        found.push((column.to_owned(), "gte", vec![value.clone()]));
    }
    if let Some(value) = &entry.to {
        found.push((column.to_owned(), "lte", vec![value.clone()]));
    }
    if let Some(null) = entry.null {
        let operator = if null { "isnull" } else { "notnull" };
        found.push((column.to_owned(), operator, Vec::new()));
    }
}

fn scope_entry(parts: &mut Vec<(String, String)>, name: &str, entry: &Entry) {
    if let Some(value) = &entry.eq {
        parts.push((name.to_owned(), value.canonical()));
    }
    if let Some(values) = &entry.many {
        let mut texts: Vec<String> = values.iter().map(Typed::canonical).collect();
        texts.sort();
        parts.push((format!("{name}[]"), texts.join("\u{1f}")));
    }
    for (suffix, bound) in [("from", &entry.from), ("to", &entry.to)] {
        if let Some(value) = bound {
            parts.push((format!("{name}.{suffix}"), value.canonical()));
        }
    }
    if let Some(null) = entry.null {
        parts.push((format!("{name}.null"), null.to_string()));
    }
}

fn boolean(name: &str, raw: &str) -> Result<bool, Problem> {
    match raw {
        "true" | "1" => Ok(true),
        "false" | "0" => Ok(false),
        _ => Err(Problem::new(name, "type", "expected true or false")),
    }
}

fn digits(raw: &str) -> &str {
    raw.strip_prefix('-').unwrap_or(raw)
}

fn integer(name: &str, raw: &str) -> Result<Typed, Problem> {
    let body = digits(raw);
    if body.is_empty() || !body.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err(Problem::new(name, "type", "expected an integer"));
    }
    if body.len() > MAX_NUMBER_DIGITS {
        return Err(Problem::new(name, "type", "the integer is out of range"));
    }
    raw.parse()
        .map(Typed::Int)
        .map_err(|_| Problem::new(name, "type", "expected an integer"))
}

fn float(name: &str, raw: &str) -> Result<Typed, Problem> {
    if !is_number(raw) {
        return Err(Problem::new(name, "type", "expected a finite number"));
    }
    match raw.parse::<f64>() {
        Ok(value) if value.is_finite() => Ok(Typed::Float(value)),
        _ => Err(Problem::new(name, "type", "expected a finite number")),
    }
}

/// `-?digits(.digits)?([eE][+-]?digits)?`, the spelling the other query
/// parameters of a route accept.
fn is_number(raw: &str) -> bool {
    let (mantissa, exponent) = match raw.split_once(['e', 'E']) {
        Some((mantissa, exponent)) => (mantissa, Some(exponent)),
        None => (raw, None),
    };
    let (whole, fraction) = match digits(mantissa).split_once('.') {
        Some((whole, fraction)) => (whole, Some(fraction)),
        None => (digits(mantissa), None),
    };
    let all_digits = |text: &str| !text.is_empty() && text.bytes().all(|b| b.is_ascii_digit());
    all_digits(whole)
        && fraction.is_none_or(all_digits)
        && exponent.is_none_or(|text| {
            all_digits(text.trim_start_matches(['+', '-']))
                && text.len() - text.trim_start_matches(['+', '-']).len() <= 1
        })
}

fn plain_text(name: &str, raw: &str, limit: usize) -> Result<(), Problem> {
    if raw.contains('\0') {
        return Err(Problem::new(name, "type", "must not contain NUL"));
    }
    if raw.chars().count() > limit {
        return Err(Problem::new(
            name,
            "too_long",
            format!("at most {limit} characters"),
        ));
    }
    Ok(())
}

fn uuid(name: &str, raw: &str) -> Result<Typed, Problem> {
    match Uuid::parse_str(raw) {
        Ok(value) if raw.len() == 36 => Ok(Typed::Uuid(value)),
        _ => Err(Problem::new(
            name,
            "type",
            "expected a UUID in 8-4-4-4-12 form",
        )),
    }
}

fn date(name: &str, raw: &str) -> Result<Typed, Problem> {
    let problem = || Problem::new(name, "type", "expected a date as YYYY-MM-DD");
    let bytes = raw.as_bytes();
    let well_formed = bytes.len() == 10
        && bytes[4] == b'-'
        && bytes[7] == b'-'
        && bytes
            .iter()
            .enumerate()
            .all(|(at, byte)| at == 4 || at == 7 || byte.is_ascii_digit());
    if !well_formed {
        return Err(problem());
    }
    let year: i32 = raw[..4].parse().map_err(|_| problem())?;
    let month: u8 = raw[5..7].parse().map_err(|_| problem())?;
    let day: u8 = raw[8..].parse().map_err(|_| problem())?;
    let month = Month::try_from(month).map_err(|_| problem())?;
    match Date::from_calendar_date(year, month, day) {
        Ok(date) if year >= 1 => Ok(Typed::Date(date)),
        _ => Err(problem()),
    }
}

fn moment(name: &str, raw: &str) -> Result<Typed, Problem> {
    let problem = || {
        Problem::new(
            name,
            "type",
            "expected an RFC 3339 date and time, for example 2026-01-31T12:00:00Z",
        )
    };
    let parsed = OffsetDateTime::parse(raw, &Rfc3339).map_err(|_| problem())?;
    let utc = parsed.to_offset(UtcOffset::UTC);
    if utc.year() < 1 || utc.year() > 9999 {
        return Err(problem());
    }
    Ok(Typed::Moment(utc))
}

fn decimal(name: &str, raw: &str) -> Result<Typed, Problem> {
    let problem = || Problem::new(name, "type", "expected a decimal number");
    let (whole, fraction) = match digits(raw).split_once('.') {
        Some((whole, fraction)) => (whole, Some(fraction)),
        None => (digits(raw), None),
    };
    let all_digits = |text: &str| !text.is_empty() && text.bytes().all(|b| b.is_ascii_digit());
    if !all_digits(whole) || !fraction.is_none_or(all_digits) {
        return Err(problem());
    }
    let count = whole.len() + fraction.map_or(0, str::len);
    if count > MAX_DECIMAL_DIGITS {
        return Err(problem());
    }
    Ok(Typed::Decimal(raw.to_owned()))
}

type Declared = (String, String, String, bool, Vec<String>);
type Reported = (String, String, String);

fn to_py(py: Python<'_>, value: &Typed) -> PyResult<Py<PyAny>> {
    Ok(match value {
        Typed::Bool(flag) => flag.into_pyobject(py)?.to_owned().into_any().unbind(),
        Typed::Int(number) => number.into_pyobject(py)?.into_any().unbind(),
        Typed::Float(number) => number.into_pyobject(py)?.into_any().unbind(),
        Typed::Text(text) => text.into_pyobject(py)?.into_any().unbind(),
        Typed::Uuid(id) => crate::pytypes::new_uuid(py, *id)?.unbind(),
        Typed::Date(day) => crate::pytypes::date_class(py)?
            .call1((day.year(), u8::from(day.month()), day.day()))?
            .unbind(),
        Typed::Moment(at) => {
            let utc = py.import("datetime")?.getattr("timezone")?.getattr("utc")?;
            crate::pytypes::datetime_class(py)?
                .call1((
                    at.year(),
                    u8::from(at.month()),
                    at.day(),
                    at.hour(),
                    at.minute(),
                    at.second(),
                    at.microsecond(),
                    utc,
                ))?
                .unbind()
        }
        Typed::Decimal(text) => crate::pytypes::decimal_class(py)?.call1((text,))?.unbind(),
    })
}

fn to_py_list(py: Python<'_>, values: &[Typed]) -> PyResult<Py<PyAny>> {
    let items = values
        .iter()
        .map(|value| to_py(py, value))
        .collect::<PyResult<Vec<_>>>()?;
    Ok(PyList::new(py, items)?.into_any().unbind())
}

fn optional(py: Python<'_>, value: &Option<Typed>) -> PyResult<Py<PyAny>> {
    match value {
        Some(value) => to_py(py, value),
        None => Ok(py.None()),
    }
}

fn declared(item: Declared) -> PyResult<Field> {
    let (name, kind, shape, nullable, choices) = item;
    let kind = Kind::parse(&kind)
        .ok_or_else(|| PyValueError::new_err(format!("unsupported filter kind {kind:?}")))?;
    let shape = Shape::parse(&shape)
        .ok_or_else(|| PyValueError::new_err(format!("unsupported filter shape {shape:?}")))?;
    Ok(Field {
        name,
        kind,
        shape,
        nullable,
        choices,
    })
}

/// The declared filters of one `FilterSet`, built once at class creation.
#[pyclass(name = "_FilterPlan", module = "hinoki.core", frozen)]
pub(crate) struct PyFilterPlan {
    spec: Spec,
}

#[pymethods]
impl PyFilterPlan {
    #[new]
    fn new(
        fields: Vec<Declared>,
        search: Vec<String>,
        ordering: Vec<String>,
        max_in: usize,
        max_text: usize,
        max_search: usize,
    ) -> PyResult<Self> {
        let fields = fields.into_iter().map(declared).collect::<PyResult<_>>()?;
        let limits = Limits {
            max_in,
            max_text,
            max_search,
        };
        Spec::new(fields, search, ordering, limits)
            .map(|spec| Self { spec })
            .map_err(PyValueError::new_err)
    }

    /// `(problems, parsed)`. `problems` is a list of `(path, code, message)`
    /// and `parsed` is `None` when it is not empty; otherwise `parsed` is
    /// `(entries, conditions, order, term, scope)`.
    fn parse<'py>(
        &self,
        py: Python<'py>,
        query: &Bound<'py, PyDict>,
        reserved: Vec<String>,
    ) -> PyResult<(Vec<Reported>, Option<Bound<'py, PyTuple>>)> {
        let pairs = query
            .iter()
            .map(|(name, values)| Ok((name.extract()?, values.extract()?)))
            .collect::<PyResult<Vec<_>>>()?;
        match self.spec.parse(&pairs, &reserved) {
            Err(problems) => Ok((report(problems), None)),
            Ok(parsed) => Ok((Vec::new(), Some(self.present(py, &parsed)?))),
        }
    }
}

impl PyFilterPlan {
    fn present<'py>(&self, py: Python<'py>, parsed: &Parsed) -> PyResult<Bound<'py, PyTuple>> {
        let entries = PyList::empty(py);
        for entry in &parsed.entries {
            let many = match &entry.many {
                Some(values) => to_py_list(py, values)?,
                None => py.None(),
            };
            entries.append((
                &self.spec.fields[entry.field].name,
                optional(py, &entry.eq)?,
                many,
                optional(py, &entry.from)?,
                optional(py, &entry.to)?,
                entry.null,
            ))?;
        }
        let conditions = self
            .spec
            .conditions(parsed)
            .into_iter()
            .map(|(column, operator, values)| Ok((column, operator, to_py_list(py, &values)?)))
            .collect::<PyResult<Vec<_>>>()?;
        let order = parsed.order.as_ref().map(|order| {
            order
                .iter()
                .map(|item| match item.strip_prefix('-') {
                    Some(column) => (column.to_owned(), true),
                    None => (item.clone(), false),
                })
                .collect::<Vec<_>>()
        });
        PyTuple::new(
            py,
            [
                entries.into_any(),
                PyTuple::new(py, conditions)?.into_any(),
                order.into_pyobject(py)?.into_any(),
                parsed.term.as_deref().into_pyobject(py)?.into_any(),
                PyBytes::new(py, &self.spec.scope(parsed)).into_any(),
            ],
        )
    }
}

fn report(problems: Vec<Problem>) -> Vec<Reported> {
    problems
        .into_iter()
        .map(|problem| (problem.path, problem.code.to_owned(), problem.message))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn field(name: &str, kind: Kind, shape: Shape) -> Field {
        Field {
            name: name.to_owned(),
            kind,
            shape,
            nullable: false,
            choices: Vec::new(),
        }
    }

    fn spec() -> Spec {
        let mut status = field("status", Kind::Str, Shape::In);
        status.choices = vec!["open".into(), "paid".into(), "overdue".into()];
        let mut closed = field("closed", Kind::DateTime, Shape::Range);
        closed.nullable = true;
        Spec::new(
            vec![
                field("paid", Kind::Bool, Shape::Eq),
                status,
                field("issued", Kind::Date, Shape::Range),
                field("amount", Kind::Int, Shape::Range),
                field("ratio", Kind::Float, Shape::Eq),
                field("ref_id", Kind::Uuid, Shape::Eq),
                field("total", Kind::Decimal, Shape::Eq),
                closed,
            ],
            vec!["reference".into(), "customer".into()],
            vec!["issued".into(), "reference".into()],
            Limits {
                max_in: 3,
                max_text: 8,
                max_search: 6,
            },
        )
        .unwrap()
    }

    fn query(pairs: &[(&str, &str)]) -> Vec<(String, Vec<String>)> {
        pairs
            .iter()
            .map(|(name, value)| ((*name).to_owned(), vec![(*value).to_owned()]))
            .collect()
    }

    fn rejected(pairs: &[(&str, &str)]) -> Vec<Problem> {
        spec().parse(&query(pairs), &[]).unwrap_err()
    }

    fn codes(pairs: &[(&str, &str)]) -> Vec<(String, &'static str)> {
        rejected(pairs)
            .into_iter()
            .map(|problem| (problem.path, problem.code))
            .collect()
    }

    #[test]
    fn every_operator_becomes_a_typed_condition() {
        let spec = spec();
        let parsed = spec
            .parse(
                &query(&[
                    ("paid", "false"),
                    ("status", "open,overdue"),
                    ("issued.from", "2026-01-31"),
                    ("amount.to", "-5"),
                    ("closed.null", "true"),
                    ("search", "acme"),
                ]),
                &[],
            )
            .unwrap();
        let conditions = spec.conditions(&parsed);
        let shape: Vec<(&str, &str, usize)> = conditions
            .iter()
            .map(|(column, op, values)| (column.as_str(), *op, values.len()))
            .collect();
        assert_eq!(
            shape,
            vec![
                ("paid", "eq", 1),
                ("status", "in", 2),
                ("issued", "gte", 1),
                ("amount", "lte", 1),
                ("closed", "isnull", 0),
                ("reference,customer", "containsany", 1),
            ]
        );
        assert_eq!(conditions[0].2, vec![Typed::Bool(false)]);
        assert_eq!(conditions[3].2, vec![Typed::Int(-5)]);
    }

    #[test]
    fn a_single_search_column_uses_contains_and_the_rest_is_bound_later() {
        let one = Spec::new(vec![], vec!["name".into()], vec![], spec().limits).unwrap();
        let parsed = one.parse(&query(&[("search", "x%_")]), &[]).unwrap();
        let conditions = one.conditions(&parsed);
        assert_eq!(conditions[0].0, "name");
        assert_eq!(conditions[0].1, "contains");
    }

    #[test]
    fn values_that_do_not_fit_the_declared_type_are_refused() {
        for (name, value) in [
            ("paid", "maybe"),
            ("amount.from", "1.5"),
            ("amount.from", "1234567890123456789"),
            ("amount.from", "-"),
            ("ratio", "NaN"),
            ("ratio", "1e"),
            ("ratio", "inf"),
            ("ref_id", "123e4567e89b12d3a456426614174000"),
            ("ref_id", "{123e4567-e89b-12d3-a456-426614174000}"),
            ("issued.from", "2026-13-01"),
            ("issued.from", "2026-02-30"),
            ("issued.from", "0000-01-01"),
            ("issued.from", "2026-1-1"),
            ("closed.from", "2026-01-01"),
            ("closed.from", "2026-01-01T00:00:00"),
            ("total", "1e3"),
            ("total", "12345678901234567890123456789012345678.1"),
        ] {
            let found = codes(&[(name, value)]);
            assert_eq!(found, vec![(name.to_owned(), "type")], "{name}={value}");
        }
    }

    #[test]
    fn numbers_dates_and_instants_are_read_exactly() {
        let spec = spec();
        let parsed = spec
            .parse(
                &query(&[
                    ("ratio", "1.5e2"),
                    ("ref_id", "123E4567-e89b-12d3-a456-426614174000"),
                    ("closed.to", "2026-01-31T13:00:00+01:00"),
                    ("total", "-10.250"),
                ]),
                &[],
            )
            .unwrap();
        let by_field = |name: &str| {
            let index = spec.fields.iter().position(|f| f.name == name).unwrap();
            parsed
                .entries
                .iter()
                .find(|e| e.field == index)
                .unwrap()
                .clone()
        };
        assert_eq!(by_field("ratio").eq, Some(Typed::Float(150.0)));
        assert_eq!(
            by_field("closed").to,
            Some(Typed::Moment(
                OffsetDateTime::parse("2026-01-31T12:00:00Z", &Rfc3339).unwrap()
            ))
        );
        assert_eq!(by_field("total").eq, Some(Typed::Decimal("-10.250".into())));
    }

    #[test]
    fn names_and_operators_outside_the_declaration_are_refused() {
        assert_eq!(
            codes(&[
                ("password", "x"),
                ("paid.from", "x"),
                ("status.to", "x"),
                ("issued", "2026-01-01"),
                ("paid.null", "true"),
                ("reference", "x"),
            ]),
            vec![
                ("password".to_owned(), "unknown"),
                ("paid.from".to_owned(), "operator"),
                ("status.to".to_owned(), "operator"),
                ("issued".to_owned(), "operator"),
                ("paid.null".to_owned(), "operator"),
                ("reference".to_owned(), "unknown"),
            ]
        );
    }

    #[test]
    fn identifiers_in_a_value_or_a_name_never_pass_through() {
        let hostile = [
            ("status", "open'; DROP TABLE invoices; --"),
            ("ordering", "issued; DROP TABLE x"),
            ("ordering", "(select 1)"),
            ("ordering", "issued desc"),
            ("ordering", "-"),
            ("paid\" or 1=1 --", "true"),
            ("1; select", "x"),
        ];
        for (name, value) in hostile {
            assert!(
                spec().parse(&query(&[(name, value)]), &[]).is_err(),
                "{name}={value}"
            );
        }
    }

    #[test]
    fn text_is_bounded_and_checked_against_its_choices() {
        assert_eq!(
            codes(&[("status", "closed")]),
            vec![("status".into(), "choice")]
        );
        assert_eq!(
            codes(&[("search", "toolong!")]),
            vec![("search".into(), "too_long")]
        );
        assert_eq!(
            codes(&[("search", "a\0b")]),
            vec![("search".into(), "type")]
        );
        assert_eq!(
            codes(&[("status", "open,open,open,open")]),
            vec![("status".into(), "too_many")]
        );
        assert_eq!(
            codes(&[("status", "open,,paid")]),
            vec![("status".into(), "type")]
        );
    }

    #[test]
    fn a_repeated_parameter_is_refused_and_an_empty_one_is_ignored() {
        let repeated = vec![(
            "paid".to_owned(),
            vec!["true".to_owned(), "false".to_owned()],
        )];
        let found = spec().parse(&repeated, &[]).unwrap_err();
        assert_eq!(found[0].code, "duplicate");
        let parsed = spec()
            .parse(
                &query(&[("paid", ""), ("search", ""), ("ordering", "")]),
                &[],
            )
            .unwrap();
        assert!(parsed.entries.is_empty() && parsed.term.is_none() && parsed.order.is_none());
    }

    #[test]
    fn ordering_keeps_the_client_sequence_inside_the_allow_list() {
        let spec = spec();
        let parsed = spec
            .parse(&query(&[("ordering", "-issued,reference")]), &[])
            .unwrap();
        assert_eq!(
            parsed.order,
            Some(vec!["-issued".into(), "reference".into()])
        );
        assert_eq!(
            codes(&[("ordering", "issued,-issued")]),
            vec![("ordering".to_owned(), "duplicate")]
        );
        assert_eq!(
            codes(&[("ordering", "paid")]),
            vec![("ordering".to_owned(), "choice")]
        );
    }

    #[test]
    fn reserved_pagination_names_are_left_alone() {
        let reserved = vec!["page".to_owned(), "page_size".to_owned()];
        let parsed = spec()
            .parse(&query(&[("page", "2"), ("paid", "true")]), &reserved)
            .unwrap();
        assert_eq!(parsed.entries.len(), 1);
        assert!(spec().parse(&query(&[("page", "2")]), &[]).is_err());
    }

    #[test]
    fn every_problem_is_reported_in_request_order() {
        assert_eq!(
            codes(&[("paid", "x"), ("nope", "1"), ("amount.to", "y")]),
            vec![
                ("paid".to_owned(), "type"),
                ("nope".to_owned(), "unknown"),
                ("amount.to".to_owned(), "type"),
            ]
        );
    }

    #[test]
    fn the_scope_follows_the_filters_and_ignores_spelling_order() {
        let spec = spec();
        let scope = |pairs: &[(&str, &str)]| {
            let parsed = spec.parse(&query(pairs), &[]).unwrap();
            spec.scope(&parsed)
        };
        assert!(scope(&[]).is_empty());
        assert!(scope(&[("ordering", "-issued")]).is_empty());
        assert_ne!(scope(&[("paid", "true")]), scope(&[("paid", "false")]));
        assert_ne!(scope(&[("paid", "true")]), scope(&[]));
        assert_eq!(
            scope(&[("status", "open,paid"), ("paid", "1")]),
            scope(&[("paid", "true"), ("status", "paid,open")])
        );
        assert_ne!(scope(&[("search", "ab")]), scope(&[("search", "abc")]));
    }

    #[test]
    fn two_declarations_of_one_parameter_are_refused() {
        let twice = Spec::new(
            vec![
                field("paid", Kind::Bool, Shape::Eq),
                field("paid", Kind::Bool, Shape::Eq),
            ],
            vec![],
            vec![],
            spec().limits,
        );
        assert!(twice.is_err());
        let shadow = Spec::new(
            vec![field("search", Kind::Str, Shape::Eq)],
            vec!["name".into()],
            vec![],
            spec().limits,
        );
        assert!(shadow.is_err());
    }
}
