//! Schema validation and instance construction.
//!
//! Python compiles a schema's closed type model into a table of definitions
//! (see `hinoki.schema`); `CompiledSchema.validate` then walks the decoded JSON
//! once and either returns the finished instance or the ordered list of
//! `(path, code, message)` problems. Semantics, codes and messages follow the
//! pure-Python engine that is kept in `tests/oracle` and compared against this
//! one: strict (no coercion), unknown keys refused by default, at most 50
//! problems.
//!
//! The walk is written once, over a [`View`] of the input: [`PyView`] reads
//! the Python objects `decode_json` built (and builds the Python result);
//! [`JsonView`] reads the GIL-free [`Json`] tree and builds [`Validated`]
//! values. Codes, messages, the error cap, the depth limit and path joining
//! are the same code for both. The GIL-free side uses a lowered copy of the
//! schema ([`NativeSchema`]) that holds no Python object; a schema whose
//! constraints cannot be lowered exactly has none.

use std::borrow::Cow;
use std::cmp::Ordering;
use std::collections::HashSet;
use std::marker::PhantomData;
use std::sync::Arc;

use pyo3::basic::CompareOp;
use pyo3::exceptions::PyUnicodeEncodeError;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString};
use regex::Regex;
use time::{Date, Month, OffsetDateTime, PrimitiveDateTime, Time, UtcOffset};
use uuid::Uuid;

use crate::request::{Json, JsonObject};

mod coerce;
mod internet;
mod scalars;

use internet::NetKind;
pub(crate) use scalars::wire_text;
pub use scalars::wire_text_of;
use scalars::{ClockTime, DecimalNumber, Duration, DurationError};

pub type SchemaWireNames = Vec<(String, String)>;
pub type SchemaInstanceFields = Option<(SchemaWireNames, Py<PyAny>)>;

const MAX_ERRORS: usize = 50;
const MAX_KEY_ECHO: usize = 64;
const MAX_DEPTH: usize = 128;
/// The digits a `Decimal` field accepts unless it declares `max_digits`.
const MAX_DECIMAL_DIGITS: i64 = 64;

pub(crate) type Problem = (String, &'static str, String);
/// The entries of a `dict` input: each key as text (`None` when it is not
/// valid Unicode) and its value.
type Entries<D> = Vec<(Option<String>, D)>;
type FieldTable<'py> = Vec<(String, String, Bound<'py, PyAny>, bool)>;

/// What the constraints of a compiled schema are made of.
pub(crate) trait Rules {
    /// The schema class an object is constructed with.
    type Class;
    /// A `pattern=` source (its `repr()` is part of the message).
    type Pattern;
    /// One `ge`/`le`/`gt`/`lt` bound.
    type Limit;
    /// The `one_of` choices.
    type Choices;
    /// The `Enum` class a checked value becomes a member of.
    type Member;
}

/// The Python objects `hinoki.schema` declared.
pub(crate) struct PyRules;

/// A schema class and, when its instances can be built without running the
/// Python constructor, how to build them.
pub(crate) struct PyClass {
    object: Py<PyAny>,
    direct: Option<Direct>,
}

/// Instances of a class with the generated constructor, no `__post_init__`, no
/// custom `__new__` and no normalizers: a record holding every field is already
/// final, so the instance is allocated and filled here.
pub(crate) struct Direct {
    /// The number of fields: a record shorter than that leaves defaults to fill.
    fields: usize,
    /// The tracking value for `__hinoki_supplied_fields__` (every field name),
    /// or `None` when the class does not track it.
    supplied: Option<Py<PyAny>>,
}

impl Rules for PyRules {
    type Class = PyClass;
    type Pattern = Py<PyAny>;
    type Limit = Py<PyAny>;
    type Choices = Vec<Py<PyAny>>;
    type Member = Py<PyAny>;
}

/// Plain Rust values lowered from exact builtin scalars, with the Python
/// text that messages show captured when the schema was compiled.
pub(crate) struct NativeRules;

impl Rules for NativeRules {
    type Class = ();
    type Pattern = String;
    type Limit = NativeLimit;
    type Choices = NativeChoices;
    type Member = ();
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub(crate) enum Number {
    Int(i64),
    Float(f64),
}

/// A lowered bound and its Python `str()`.
pub(crate) struct NativeLimit {
    value: LimitValue,
    shown: String,
}

/// A bound of a number field, or of a `Decimal` field (compared exactly).
enum LimitValue {
    Number(Number),
    Decimal(DecimalNumber),
}

#[derive(Debug, Clone, PartialEq)]
enum Choice {
    /// `bool` choices are stored as `0`/`1`: Python compares them as ints.
    Number(Number),
    Str(String),
}

/// Lowered `one_of` choices and the message text Python would show.
pub(crate) struct NativeChoices {
    values: Vec<Choice>,
    shown: String,
}

enum Kind<R: Rules> {
    Str,
    Int,
    Float,
    Bool,
    Uuid,
    Datetime,
    Date,
    Time,
    Timedelta,
    Decimal,
    Bytes,
    /// An `ipaddress` address or network, read from text.
    Net(NetKind),
    /// `Any` / `object`: every value is accepted as it is.
    Any,
    /// A plain union: the variants in declared order, the first that accepts
    /// the value wins.
    AnyOf(Vec<Node<R>>),
    List(Box<Node<R>>),
    /// `tuple[X, Y]`: one node per position, the array length fixed by the
    /// node's `min_length` and `max_length`.
    Tuple(Vec<Node<R>>),
    /// `dict[str, X]`: the key node (always a string) and the value node.
    Map {
        key: Box<Node<R>>,
        value: Box<Node<R>>,
    },
    Schema(usize),
    TaggedUnion {
        discriminator: String,
        discriminator_wire_name: String,
        variants: Vec<(String, usize)>,
    },
}

/// The container a checked array becomes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Collection {
    Set,
    Frozenset,
    Tuple,
}

struct Node<R: Rules> {
    kind: Kind<R>,
    /// The container that replaces the checked array.
    collection: Option<Collection>,
    nullable: bool,
    min_length: Option<i64>,
    max_length: Option<i64>,
    /// The framework bound used on wire input when `max_length` is unset (the
    /// entries of a dict, the characters of its keys).
    input_max_length: Option<i64>,
    pattern: Option<(R::Pattern, Regex)>,
    ge: Option<R::Limit>,
    le: Option<R::Limit>,
    gt: Option<R::Limit>,
    lt: Option<R::Limit>,
    one_of: Option<R::Choices>,
    /// The `Enum` class whose member replaces the value once it is checked.
    member: Option<R::Member>,
    /// `Decimal` only: the digit counts the written value may have.
    max_digits: Option<i64>,
    decimal_places: Option<i64>,
    /// `int` and `Decimal` only: the step the value must be a multiple of.
    multiple_of: Option<R::Limit>,
}

struct Field<R: Rules> {
    name: String,
    wire_name: String,
    node: Node<R>,
    required: bool,
}

struct Definition<R: Rules> {
    class: R::Class,
    fields: Vec<Field<R>>,
    forbid_extra: bool,
}

/// A schema (and everything reachable from it) ready to validate.
#[pyclass(name = "CompiledSchema", module = "hinoki.core")]
pub struct Compiled {
    definitions: Vec<Definition<PyRules>>,
    root: usize,
    uuid: Py<PyAny>,
    from_iso: Py<PyAny>,
    unset: Py<PyAny>,
    native: Option<Arc<NativeSchema>>,
}

/// The GIL-free copy of a [`Compiled`] schema.
pub(crate) struct NativeSchema {
    definitions: Vec<Definition<NativeRules>>,
    root: usize,
}

/// How one validation runs (the keyword arguments of `validate`).
pub(crate) struct Options {
    pub(crate) construct: bool,
    pub(crate) use_aliases: bool,
    pub(crate) allow_naive_datetime: bool,
    pub(crate) skip_fields: HashSet<String>,
    /// Wire input: the default bounds of dict fields apply.
    pub(crate) input_bounds: bool,
}

/// A value the GIL-free path accepted: what the Python path returns with
/// `construct=False`, as plain Rust data. Objects list their fields by
/// Python name in declaration order.
#[derive(Debug, Clone, PartialEq)]
pub(crate) enum Validated {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(String),
    Uuid(Uuid),
    /// A datetime with its parsed UTC offset.
    Datetime(OffsetDateTime),
    /// A datetime without offset (only with `allow_naive_datetime`).
    NaiveDatetime(PrimitiveDateTime),
    Date(Date),
    Time(ClockTime),
    Duration(Duration),
    /// The decimal text as written (its scale is part of the value).
    Decimal(String),
    Bytes(Vec<u8>),
    /// The text as written; the kind says which `ipaddress` class it becomes.
    Net(NetKind, String),
    List(Vec<Validated>),
    /// A schema object, or the entries of a `dict` field in input order.
    Object(Vec<(String, Validated)>),
}

/// The GIL-free path met an input whose Python result it does not reproduce
/// exactly (an integer outside `i64`, a datetime Python may read differently);
/// the caller must hand the input to the Python path instead.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct Defer(pub(crate) &'static str);

enum Abort<E> {
    TooMany,
    Depth,
    View(E),
}

type Checked<V> = Result<Option<<V as View>::Out>, Abort<<V as View>::Error>>;
type Outcome<V> = Result<(Option<<V as View>::Out>, Vec<Problem>), <V as View>::Error>;

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

fn parse_node(raw: &Bound<'_, PyAny>) -> PyResult<Node<PyRules>> {
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
        "date" => Kind::Date,
        "time" => Kind::Time,
        "timedelta" => Kind::Timedelta,
        "decimal" => Kind::Decimal,
        "bytes" => Kind::Bytes,
        "ipv4" => Kind::Net(NetKind::V4),
        "ipv6" => Kind::Net(NetKind::V6),
        "ipv4network" => Kind::Net(NetKind::V4Network),
        "ipv6network" => Kind::Net(NetKind::V6Network),
        "list" => {
            let inner = item.ok_or_else(|| compile_error("list type without an item type"))?;
            Kind::List(Box::new(parse_node(&inner)?))
        }
        "any" => Kind::Any,
        "anyof" => {
            let variants = constraints
                .get_item("items")?
                .ok_or_else(|| compile_error("union type without variants"))?;
            let mut nodes = Vec::new();
            for variant in variants.try_iter()? {
                nodes.push(parse_node(&variant?)?);
            }
            Kind::AnyOf(nodes)
        }
        "tuple" => {
            let positions = constraints
                .get_item("items")?
                .ok_or_else(|| compile_error("tuple type without item types"))?;
            let mut nodes = Vec::new();
            for position in positions.try_iter()? {
                nodes.push(parse_node(&position?)?);
            }
            Kind::Tuple(nodes)
        }
        "dict" => {
            let value = item.ok_or_else(|| compile_error("dict type without a value type"))?;
            let key = constraints
                .get_item("key")?
                .ok_or_else(|| compile_error("dict type without a key type"))?;
            Kind::Map {
                key: Box::new(parse_node(&key)?),
                value: Box::new(parse_node(&value)?),
            }
        }
        "schema" => {
            Kind::Schema(index.ok_or_else(|| compile_error("schema type without a schema"))?)
        }
        "union" => {
            let discriminator = required_string(&constraints, "discriminator")?;
            let discriminator_wire_name = required_string(&constraints, "discriminator_wire_name")?;
            let variants = required_variants(&constraints)?;
            Kind::TaggedUnion {
                discriminator,
                discriminator_wire_name,
                variants,
            }
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
    let collection = match constraints.get_item("collection")? {
        Some(name) => Some(match name.extract::<String>()?.as_str() {
            "set" => Collection::Set,
            "frozenset" => Collection::Frozenset,
            "tuple" => Collection::Tuple,
            other => return Err(compile_error(format!("unknown collection {other:?}"))),
        }),
        None => None,
    };
    Ok(Node {
        kind,
        collection,
        nullable,
        min_length: optional_int(&constraints, "min_length")?,
        max_length: optional_int(&constraints, "max_length")?,
        input_max_length: optional_int(&constraints, "input_max_length")?,
        pattern,
        ge: optional_object(&constraints, "ge")?,
        le: optional_object(&constraints, "le")?,
        gt: optional_object(&constraints, "gt")?,
        lt: optional_object(&constraints, "lt")?,
        one_of: optional_choices(&constraints)?,
        member: optional_object(&constraints, "enum")?,
        max_digits: optional_int(&constraints, "max_digits")?,
        decimal_places: optional_int(&constraints, "decimal_places")?,
        multiple_of: optional_object(&constraints, "multiple_of")?,
    })
}

fn required_string(constraints: &Bound<'_, PyDict>, key: &str) -> PyResult<String> {
    constraints
        .get_item(key)?
        .ok_or_else(|| compile_error(format!("union type without {key}")))?
        .extract()
}

fn required_variants(constraints: &Bound<'_, PyDict>) -> PyResult<Vec<(String, usize)>> {
    let values = constraints
        .get_item("variants")?
        .ok_or_else(|| compile_error("union type without variants"))?;
    let mut variants = Vec::new();
    for value in values.try_iter()? {
        let variant = value?;
        let (tag, index): (String, usize) = variant.extract()?;
        if variants.iter().any(|(known, _)| known == &tag) {
            return Err(compile_error("union type has duplicate discriminator tags"));
        }
        variants.push((tag, index));
    }
    if variants.len() < 2 {
        return Err(compile_error("union type needs at least two variants"));
    }
    Ok(variants)
}

/// Build a validator from `definitions`: a list of
/// `(class, [(name, wire_name, node, required), ...], forbid_extra)`; nested
/// schemas refer to a definition by index.
#[pyfunction]
#[pyo3(signature = (definitions, root, uuid_class, from_iso, unset, direct=None))]
pub fn compile_schemas(
    py: Python<'_>,
    definitions: Vec<(Py<PyAny>, FieldTable<'_>, bool)>,
    root: usize,
    uuid_class: Py<PyAny>,
    from_iso: Py<PyAny>,
    unset: Py<PyAny>,
    direct: Option<Vec<(usize, Option<Py<PyAny>>)>>,
) -> PyResult<Compiled> {
    let mut direct = direct.unwrap_or_default();
    let mut built = Vec::with_capacity(definitions.len());
    for (position, (class, fields, forbid_extra)) in definitions.into_iter().enumerate() {
        let direct = direct
            .iter()
            .position(|(index, _)| *index == position)
            .map(|at| Direct {
                fields: fields.len(),
                supplied: direct.swap_remove(at).1,
            });
        let mut parsed = Vec::with_capacity(fields.len());
        for (name, wire_name, node, required) in fields {
            parsed.push(Field {
                name,
                wire_name,
                node: parse_node(&node)?,
                required,
            });
        }
        built.push(Definition {
            class: PyClass {
                object: class,
                direct,
            },
            fields: parsed,
            forbid_extra,
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
    let native = lower_schemas(py, &built, root);
    Ok(Compiled {
        definitions: built,
        root,
        uuid: uuid_class,
        from_iso,
        unset,
        native,
    })
}

/// A constraint that has no exact plain-Rust equivalent.
struct Unlowerable;

/// The GIL-free copy of `definitions`, or `None` when a constraint is not an
/// exact builtin scalar the Rust side compares exactly as Python does.
fn lower_schemas(
    py: Python<'_>,
    definitions: &[Definition<PyRules>],
    root: usize,
) -> Option<Arc<NativeSchema>> {
    let definitions = definitions
        .iter()
        .map(|definition| lower_definition(py, definition))
        .collect::<Result<Vec<_>, Unlowerable>>()
        .ok()?;
    Some(Arc::new(NativeSchema { definitions, root }))
}

fn lower_definition(
    py: Python<'_>,
    definition: &Definition<PyRules>,
) -> Result<Definition<NativeRules>, Unlowerable> {
    let fields = definition
        .fields
        .iter()
        .map(|field| {
            Ok(Field {
                name: field.name.clone(),
                wire_name: field.wire_name.clone(),
                node: lower_node(py, &field.node)?,
                required: field.required,
            })
        })
        .collect::<Result<Vec<_>, Unlowerable>>()?;
    Ok(Definition {
        class: (),
        fields,
        forbid_extra: definition.forbid_extra,
    })
}

fn lower_kind(py: Python<'_>, kind: &Kind<PyRules>) -> Result<Kind<NativeRules>, Unlowerable> {
    Ok(match kind {
        Kind::Str => Kind::Str,
        Kind::Int => Kind::Int,
        Kind::Float => Kind::Float,
        Kind::Bool => Kind::Bool,
        Kind::Uuid => Kind::Uuid,
        Kind::Datetime => Kind::Datetime,
        Kind::Date => Kind::Date,
        Kind::Time => Kind::Time,
        Kind::Timedelta => Kind::Timedelta,
        Kind::Decimal => Kind::Decimal,
        Kind::Bytes => Kind::Bytes,
        Kind::Net(kind) => Kind::Net(*kind),
        Kind::List(item) => Kind::List(Box::new(lower_node(py, item)?)),
        Kind::Tuple(_) | Kind::Any | Kind::AnyOf(_) => return Err(Unlowerable),
        Kind::Map { key, value } => Kind::Map {
            key: Box::new(lower_node(py, key)?),
            value: Box::new(lower_node(py, value)?),
        },
        Kind::Schema(index) => Kind::Schema(*index),
        Kind::TaggedUnion {
            discriminator,
            discriminator_wire_name,
            variants,
        } => Kind::TaggedUnion {
            discriminator: discriminator.clone(),
            discriminator_wire_name: discriminator_wire_name.clone(),
            variants: variants.clone(),
        },
    })
}

fn lower_node(py: Python<'_>, node: &Node<PyRules>) -> Result<Node<NativeRules>, Unlowerable> {
    let kind = lower_kind(py, &node.kind)?;
    let scalar = matches!(kind, Kind::Str | Kind::Int | Kind::Float | Kind::Bool);
    // Only strings consult `pattern` and only numbers their bounds; a
    // constraint the walk never reads needs no Rust equivalent.
    let numeric = matches!(kind, Kind::Int | Kind::Float | Kind::Decimal);
    let decimal = matches!(kind, Kind::Decimal);
    let pattern = match &node.pattern {
        Some((source, regex)) if matches!(kind, Kind::Str) => {
            Some((lower_pattern(source.bind(py))?, regex.clone()))
        }
        _ => None,
    };
    let limit = |limit: &Option<Py<PyAny>>| match limit {
        Some(limit) if decimal => lower_decimal_limit(py, limit).map(Some),
        Some(limit) if numeric => lower_limit(py, limit).map(Some),
        _ => Ok(None),
    };
    let one_of = match &node.one_of {
        // Other kinds compare Python objects (UUID, datetime, dict, instance).
        Some(_) if !scalar => return Err(Unlowerable),
        Some(choices) => Some(lower_choices(py, choices)?),
        None => None,
    };
    // A member is a Python object; the GIL-free walk cannot build one.
    if node.member.is_some() || node.collection.is_some() || matches!(kind, Kind::Tuple(_)) {
        return Err(Unlowerable);
    }
    Ok(Node {
        kind,
        collection: None,
        nullable: node.nullable,
        min_length: node.min_length,
        max_length: node.max_length,
        input_max_length: node.input_max_length,
        pattern,
        ge: limit(&node.ge)?,
        le: limit(&node.le)?,
        gt: limit(&node.gt)?,
        lt: limit(&node.lt)?,
        one_of,
        member: None,
        max_digits: node.max_digits,
        decimal_places: node.decimal_places,
        multiple_of: limit(&node.multiple_of)?,
    })
}

fn lower_pattern(source: &Bound<'_, PyAny>) -> Result<String, Unlowerable> {
    if !source.is_exact_instance_of::<PyString>() {
        return Err(Unlowerable);
    }
    source
        .repr()
        .map(|shown| shown.to_string())
        .map_err(|_| Unlowerable)
}

fn exact_number(value: &Bound<'_, PyAny>) -> Result<Number, Unlowerable> {
    if value.is_exact_instance_of::<PyInt>() {
        return value
            .extract::<i64>()
            .map(Number::Int)
            .map_err(|_| Unlowerable);
    }
    if value.is_exact_instance_of::<PyFloat>() {
        return value
            .extract::<f64>()
            .map(Number::Float)
            .map_err(|_| Unlowerable);
    }
    Err(Unlowerable)
}

fn lower_limit(py: Python<'_>, limit: &Py<PyAny>) -> Result<NativeLimit, Unlowerable> {
    let limit = limit.bind(py);
    let value = LimitValue::Number(exact_number(limit)?);
    let shown = limit.str().map_err(|_| Unlowerable)?.to_string();
    Ok(NativeLimit { value, shown })
}

/// Digits past which a `Decimal` bound is left to Python's own comparison
/// rather than written out.
const MAX_LOWERED_DECIMAL_DIGITS: i64 = 1100;

/// A `Decimal` field's bound: an exact `int`, or an exact finite `Decimal`
/// of moderate size, written out in fixed-point.
fn lower_decimal_limit(py: Python<'_>, limit: &Py<PyAny>) -> Result<NativeLimit, Unlowerable> {
    let limit = limit.bind(py);
    let shown = limit.str().map_err(|_| Unlowerable)?.to_string();
    let text: String = if limit.is_exact_instance_of::<PyInt>() {
        shown.clone()
    } else if limit
        .get_type()
        .is(crate::pytypes::decimal_class(py).map_err(|_| Unlowerable)?)
    {
        let adjusted: i64 = limit
            .call_method0("adjusted")
            .and_then(|v| v.extract())
            .map_err(|_| Unlowerable)?;
        if adjusted.abs() > MAX_LOWERED_DECIMAL_DIGITS {
            return Err(Unlowerable);
        }
        let exponent: i64 = limit
            .call_method0("as_tuple")
            .and_then(|t| t.getattr("exponent"))
            .and_then(|v| v.extract())
            .map_err(|_| Unlowerable)?;
        if exponent.abs() > MAX_LOWERED_DECIMAL_DIGITS {
            return Err(Unlowerable);
        }
        limit
            .call_method1("__format__", ("f",))
            .and_then(|v| v.extract())
            .map_err(|_| Unlowerable)?
    } else {
        return Err(Unlowerable);
    };
    let value = DecimalNumber::parse(&text).ok_or(Unlowerable)?;
    Ok(NativeLimit {
        value: LimitValue::Decimal(value),
        shown,
    })
}

fn exact_choice(value: &Bound<'_, PyAny>) -> Result<Choice, Unlowerable> {
    if let Ok(flag) = value.cast_exact::<PyBool>() {
        return Ok(Choice::Number(Number::Int(i64::from(flag.is_true()))));
    }
    if value.is_exact_instance_of::<PyString>() {
        // Fails for lone surrogates, which a Rust string cannot hold.
        return value
            .extract::<String>()
            .map(Choice::Str)
            .map_err(|_| Unlowerable);
    }
    exact_number(value).map(Choice::Number)
}

fn lower_choices(py: Python<'_>, choices: &[Py<PyAny>]) -> Result<NativeChoices, Unlowerable> {
    let values = choices
        .iter()
        .map(|choice| exact_choice(choice.bind(py)))
        .collect::<Result<Vec<_>, Unlowerable>>()?;
    Ok(NativeChoices {
        values,
        shown: shown_choices(py, choices),
    })
}

/// The `one_of` message text: the `repr()` of the first ten choices.
fn shown_choices(py: Python<'_>, choices: &[Py<PyAny>]) -> String {
    let shown: Vec<String> = choices
        .iter()
        .take(10)
        .map(|c| c.bind(py).repr().map(|r| r.to_string()).unwrap_or_default())
        .collect();
    shown.join(", ")
}

/// For a `Schema` instance: its field names in declaration order and the
/// `UNSET` sentinel (fields holding it are not serialised). The class is
/// compiled on first use. `None` when `value` is not a schema instance (or,
/// with `honour_custom_dump`, when its class overrides `dump()`).
pub fn instance_fields(
    value: &Bound<'_, PyAny>,
    honour_custom_dump: bool,
    by_alias: bool,
) -> PyResult<SchemaInstanceFields> {
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
        .map(|field| {
            (
                field.name.clone(),
                if by_alias {
                    field.wire_name.clone()
                } else {
                    field.name.clone()
                },
            )
        })
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

fn refers_outside<R: Rules>(node: &Node<R>, known: usize) -> bool {
    match &node.kind {
        Kind::Schema(index) => *index >= known,
        Kind::List(inner) | Kind::Map { value: inner, .. } => refers_outside(inner, known),
        Kind::Tuple(positions) | Kind::AnyOf(positions) => {
            positions.iter().any(|inner| refers_outside(inner, known))
        }
        Kind::TaggedUnion { variants, .. } => variants.iter().any(|(_, index)| *index >= known),
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

/// How the validator reads its input and builds its result. Every method
/// answers one question the walk asks, in the order the walk asks it, so a
/// view cannot change which problems are found or their order.
trait View {
    type Rules: Rules;
    /// One input value.
    type Data;
    /// An input string.
    type Str;
    /// An input object.
    type Object;
    /// A string key of an input object.
    type Key;
    /// An input array.
    type List;
    /// One accepted value.
    type Out;
    /// An accepted array being built.
    type ListOut;
    /// The accepted fields of one object being built.
    type Record;
    type Error;

    fn is_null(&self, data: &Self::Data) -> bool;
    fn null(&self, data: &Self::Data) -> Self::Out;
    fn string(&self, data: &Self::Data) -> Option<Self::Str>;
    /// Length in code points.
    fn char_count(&self, text: &Self::Str) -> Result<usize, Self::Error>;
    fn text<'s>(&self, text: &'s Self::Str) -> Result<Cow<'s, str>, Self::Error>;
    fn string_out(&self, text: Self::Str) -> Self::Out;
    /// `None` for anything but an integer.
    fn integer(&self, data: &Self::Data) -> Result<Option<Self::Out>, Self::Error>;
    /// `None` for anything but an integer or a float; `Some(None)` when it
    /// has no `f64` value.
    fn number(&self, data: &Self::Data) -> Result<Option<Option<f64>>, Self::Error>;
    fn float(&self, value: f64) -> Self::Out;
    fn boolean(&self, data: &Self::Data) -> Option<Self::Out>;
    /// A canonical UUID string (`text` is its content).
    fn uuid(&self, data: &Self::Data, text: &str) -> Result<Self::Out, Self::Error>;
    /// An RFC 3339-shaped string: `None` when it is not a valid datetime,
    /// otherwise the value and whether it has no UTC offset.
    fn datetime(
        &self,
        data: &Self::Data,
        text: &str,
    ) -> Result<Option<(Self::Out, bool)>, Self::Error>;
    /// The text of a decimal input: a string as it is, an integer in
    /// decimal; `None` for anything else. An integer too long for Python to
    /// print gives a text past every digit limit.
    fn decimal_text(&self, data: &Self::Data) -> Result<Option<String>, Self::Error>;
    /// The value of a text the walk has validated as `wire`.
    fn wire(&self, wire: Wire, text: &str) -> Result<Self::Out, Self::Error>;
    fn list(&self, data: &Self::Data) -> Option<Self::List>;
    fn list_len(&self, list: &Self::List) -> usize;
    fn items(list: &Self::List) -> impl Iterator<Item = Self::Data>;
    fn new_list(&self) -> Self::ListOut;
    fn push(&self, out: &mut Self::ListOut, value: Self::Out) -> Result<(), Self::Error>;
    fn close_list(&self, out: Self::ListOut) -> Self::Out;
    fn object(&self, data: &Self::Data) -> Option<Self::Object>;
    fn non_string_keys(&self, object: &Self::Object) -> usize;
    fn string_keys(&self, object: &Self::Object) -> Vec<Self::Key>;
    /// The entries of an object whose keys are all strings, in order, each
    /// key as text or `None` when it is not valid Unicode (a lone surrogate).
    fn entries(&self, object: &Self::Object) -> Result<Entries<Self::Data>, Self::Error>;
    fn key_text<'k>(&self, key: &'k Self::Key) -> Result<Cow<'k, str>, Self::Error>;
    fn contains(&self, object: &Self::Object, key: &str) -> Result<bool, Self::Error>;
    fn get(&self, object: &Self::Object, key: &str) -> Result<Option<Self::Data>, Self::Error>;
    fn new_record(&self) -> Self::Record;
    fn set_field(
        &self,
        record: &mut Self::Record,
        name: &str,
        value: Self::Out,
    ) -> Result<(), Self::Error>;
    fn close_record(
        &self,
        class: &<Self::Rules as Rules>::Class,
        record: Self::Record,
        path: &str,
        construct: bool,
    ) -> Result<Self::Out, Self::Error>;
    /// The accepted entries of a `dict` field, as a plain mapping.
    fn close_map(&self, record: Self::Record) -> Self::Out;
    /// `Some(shown limit)` when `value` violates `limit` under `op`.
    fn limit_fails(
        &self,
        value: &Self::Out,
        limit: &<Self::Rules as Rules>::Limit,
        op: CompareOp,
    ) -> Result<Option<String>, Self::Error>;
    /// `Some(shown step)` when `value` is not a multiple of `step`.
    fn multiple_fails(
        &self,
        value: &Self::Out,
        step: &<Self::Rules as Rules>::Limit,
    ) -> Result<Option<String>, Self::Error>;
    fn is_choice(
        &self,
        value: &Self::Out,
        choices: &<Self::Rules as Rules>::Choices,
    ) -> Result<bool, Self::Error>;
    fn choices_shown(&self, choices: &<Self::Rules as Rules>::Choices) -> String;
    /// The member of an `Enum` class named by an accepted value.
    fn member(
        &self,
        class: &<Self::Rules as Rules>::Member,
        value: Self::Out,
    ) -> Result<Self::Out, Self::Error>;
    /// The container a checked array becomes (duplicates of a set collapse).
    fn collect(&self, collection: Collection, value: Self::Out) -> Result<Self::Out, Self::Error>;
    /// An `Any` field's value: the data as it was given.
    fn anything(&self, data: &Self::Data) -> Result<Self::Out, Self::Error>;
    fn pattern_shown(
        &self,
        pattern: &<Self::Rules as Rules>::Pattern,
    ) -> Result<String, Self::Error>;
}

/// A text the walk has checked as one of the scalar wire forms.
enum Wire {
    Date(Date),
    Time(ClockTime),
    Duration(Duration),
    Decimal,
    Bytes(Vec<u8>),
    Net(NetKind),
}

/// Reads Python objects and builds the Python result (`CompiledSchema.validate`).
struct PyView<'a, 'py> {
    py: Python<'py>,
    uuid: &'a Py<PyAny>,
    from_iso: &'a Py<PyAny>,
}

impl<'py> View for PyView<'_, 'py> {
    type Rules = PyRules;
    type Data = Bound<'py, PyAny>;
    type Str = Bound<'py, PyString>;
    type Object = Bound<'py, PyDict>;
    type Key = Bound<'py, PyString>;
    type List = Bound<'py, PyList>;
    type Out = Bound<'py, PyAny>;
    type ListOut = Bound<'py, PyList>;
    type Record = Bound<'py, PyDict>;
    type Error = PyErr;

    fn is_null(&self, data: &Self::Data) -> bool {
        data.is_none()
    }

    fn null(&self, data: &Self::Data) -> Self::Out {
        data.clone()
    }

    fn string(&self, data: &Self::Data) -> Option<Self::Str> {
        data.cast::<PyString>().ok().cloned()
    }

    fn char_count(&self, text: &Self::Str) -> PyResult<usize> {
        text.len()
    }

    fn text<'s>(&self, text: &'s Self::Str) -> PyResult<Cow<'s, str>> {
        text.to_cow()
    }

    fn string_out(&self, text: Self::Str) -> Self::Out {
        text.into_any()
    }

    fn integer(&self, data: &Self::Data) -> PyResult<Option<Self::Out>> {
        Ok(data.is_exact_instance_of::<PyInt>().then(|| data.clone()))
    }

    fn number(&self, data: &Self::Data) -> PyResult<Option<Option<f64>>> {
        if !(data.is_exact_instance_of::<PyInt>() || data.is_exact_instance_of::<PyFloat>()) {
            return Ok(None);
        }
        Ok(Some(data.extract::<f64>().ok()))
    }

    fn float(&self, value: f64) -> Self::Out {
        PyFloat::new(self.py, value).into_any()
    }

    fn boolean(&self, data: &Self::Data) -> Option<Self::Out> {
        data.is_exact_instance_of::<PyBool>().then(|| data.clone())
    }

    fn uuid(&self, data: &Self::Data, _text: &str) -> PyResult<Self::Out> {
        self.uuid.bind(self.py).call1((data,))
    }

    fn datetime(&self, data: &Self::Data, _text: &str) -> PyResult<Option<(Self::Out, bool)>> {
        let parsed = match self.from_iso.bind(self.py).call1((data,)) {
            Ok(value) => value,
            Err(err) if err.is_instance_of::<pyo3::exceptions::PyValueError>(self.py) => {
                return Ok(None);
            }
            Err(err) => return Err(err),
        };
        let naive = parsed.getattr("tzinfo")?.is_none();
        Ok(Some((parsed, naive)))
    }

    fn decimal_text(&self, data: &Self::Data) -> PyResult<Option<String>> {
        if let Ok(text) = data.cast::<PyString>() {
            return Ok(Some(text.to_cow()?.into_owned()));
        }
        if !data.is_exact_instance_of::<PyInt>() {
            return Ok(None);
        }
        match data.str() {
            Ok(text) => Ok(Some(text.to_string())),
            Err(err) if err.is_instance_of::<pyo3::exceptions::PyValueError>(self.py) => {
                Ok(Some("9".repeat(scalars::DECIMAL_DIGITS_CEILING + 1)))
            }
            Err(err) => Err(err),
        }
    }

    fn wire(&self, wire: Wire, text: &str) -> PyResult<Self::Out> {
        let py = self.py;
        match wire {
            Wire::Date(_) => crate::pytypes::date_class(py)?.call_method1("fromisoformat", (text,)),
            Wire::Time(_) => crate::pytypes::time_class(py)?.call_method1("fromisoformat", (text,)),
            Wire::Duration(span) => {
                crate::pytypes::timedelta_class(py)?.call1((span.days, span.seconds, span.micros))
            }
            Wire::Decimal => crate::pytypes::decimal_class(py)?.call1((text,)),
            Wire::Bytes(raw) => Ok(PyBytes::new(py, &raw).into_any()),
            Wire::Net(kind) => py
                .import("ipaddress")?
                .getattr(kind.class_name())?
                .call1((text,)),
        }
    }

    fn list(&self, data: &Self::Data) -> Option<Self::List> {
        data.cast::<PyList>().ok().cloned()
    }

    fn list_len(&self, list: &Self::List) -> usize {
        list.len()
    }

    fn items(list: &Self::List) -> impl Iterator<Item = Self::Data> {
        list.iter()
    }

    fn new_list(&self) -> Self::ListOut {
        PyList::empty(self.py)
    }

    fn push(&self, out: &mut Self::ListOut, value: Self::Out) -> PyResult<()> {
        out.append(value)
    }

    fn close_list(&self, out: Self::ListOut) -> Self::Out {
        out.into_any()
    }

    fn object(&self, data: &Self::Data) -> Option<Self::Object> {
        data.cast::<PyDict>().ok().cloned()
    }

    fn non_string_keys(&self, object: &Self::Object) -> usize {
        object
            .keys()
            .iter()
            .filter(|key| key.cast::<PyString>().is_err())
            .count()
    }

    fn string_keys(&self, object: &Self::Object) -> Vec<Self::Key> {
        object
            .keys()
            .iter()
            .filter_map(|key| key.cast_into::<PyString>().ok())
            .collect()
    }

    fn key_text<'k>(&self, key: &'k Self::Key) -> PyResult<Cow<'k, str>> {
        key.to_cow()
    }

    fn entries(&self, object: &Self::Object) -> PyResult<Entries<Self::Data>> {
        let mut found = Vec::with_capacity(object.len());
        for (key, value) in object.iter() {
            let Ok(text) = key.cast::<PyString>() else {
                continue;
            };
            let text = match text.to_cow() {
                Ok(text) => Some(text.into_owned()),
                Err(err) if err.is_instance_of::<PyUnicodeEncodeError>(self.py) => None,
                Err(err) => return Err(err),
            };
            found.push((text, value));
        }
        Ok(found)
    }

    fn contains(&self, object: &Self::Object, key: &str) -> PyResult<bool> {
        object.contains(key)
    }

    fn get(&self, object: &Self::Object, key: &str) -> PyResult<Option<Self::Data>> {
        object.get_item(key)
    }

    fn new_record(&self) -> Self::Record {
        PyDict::new(self.py)
    }

    fn set_field(&self, record: &mut Self::Record, name: &str, value: Self::Out) -> PyResult<()> {
        record.set_item(name, value)
    }

    fn close_record(
        &self,
        class: &PyClass,
        record: Self::Record,
        path: &str,
        construct: bool,
    ) -> PyResult<Self::Out> {
        if !construct {
            return Ok(record.into_any());
        }
        let class_object = class.object.bind(self.py);
        if let Some(direct) = class.direct.as_ref().filter(|d| record.len() == d.fields) {
            return build_direct(self.py, class_object, direct, &record);
        }
        class_object.call_method1("__hinoki_construct_validated__", (record, path))
    }

    fn close_map(&self, record: Self::Record) -> Self::Out {
        record.into_any()
    }

    fn limit_fails(
        &self,
        value: &Self::Out,
        limit: &Py<PyAny>,
        op: CompareOp,
    ) -> PyResult<Option<String>> {
        let limit = limit.bind(self.py);
        if value.rich_compare(limit, op)?.is_truthy()? {
            return Ok(None);
        }
        Ok(Some(limit.str()?.to_string()))
    }

    fn multiple_fails(&self, value: &Self::Out, step: &Py<PyAny>) -> PyResult<Option<String>> {
        let step = step.bind(self.py);
        // Exact fractions: `Decimal` arithmetic would round past its precision.
        let ratio =
            |number: &Bound<'py, PyAny>| -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyAny>)> {
                number.call_method0("as_integer_ratio")?.extract()
            };
        let (value_n, value_d) = ratio(value)?;
        let (step_n, step_d) = ratio(step)?;
        let left = value_n.mul(&step_d)?;
        let right = step_n.mul(&value_d)?;
        if left.rem(&right)?.is_truthy()? {
            return Ok(Some(step.str()?.to_string()));
        }
        Ok(None)
    }

    fn is_choice(&self, value: &Self::Out, choices: &Vec<Py<PyAny>>) -> PyResult<bool> {
        for choice in choices {
            if value.eq(choice.bind(self.py))? {
                return Ok(true);
            }
        }
        Ok(false)
    }

    fn choices_shown(&self, choices: &Vec<Py<PyAny>>) -> String {
        shown_choices(self.py, choices)
    }

    fn member(&self, class: &Py<PyAny>, value: Self::Out) -> PyResult<Self::Out> {
        class.bind(self.py).call1((value,))
    }

    fn collect(&self, collection: Collection, value: Self::Out) -> PyResult<Self::Out> {
        let builtin = match collection {
            Collection::Set => self.py.get_type::<pyo3::types::PySet>().into_any(),
            Collection::Frozenset => self.py.get_type::<pyo3::types::PyFrozenSet>().into_any(),
            Collection::Tuple => self.py.get_type::<pyo3::types::PyTuple>().into_any(),
        };
        builtin.call1((value,))
    }

    fn anything(&self, data: &Self::Data) -> PyResult<Self::Out> {
        Ok(data.clone())
    }

    fn pattern_shown(&self, pattern: &Py<PyAny>) -> PyResult<String> {
        Ok(pattern.bind(self.py).repr()?.to_string())
    }
}

/// Reads the GIL-free [`Json`] tree and builds [`Validated`] values.
struct JsonView<'v> {
    input: PhantomData<&'v Json>,
}

impl Number {
    fn of(value: &Validated) -> Option<Number> {
        match value {
            Validated::Int(int) => Some(Number::Int(*int)),
            Validated::Float(float) => Some(Number::Float(*float)),
            Validated::Bool(flag) => Some(Number::Int(i64::from(*flag))),
            _ => None,
        }
    }
}

const TWO_POW_63: f64 = 9_223_372_036_854_775_808.0;

/// Python's exact comparison of an `int` with a `float` (no rounding).
fn compare_int_float(int: i64, float: f64) -> Option<Ordering> {
    if float.is_nan() {
        return None;
    }
    if float >= TWO_POW_63 {
        return Some(Ordering::Less);
    }
    if float < -TWO_POW_63 {
        return Some(Ordering::Greater);
    }
    let whole = float.trunc();
    // In range and integral, so the cast is exact.
    #[expect(
        clippy::cast_possible_truncation,
        reason = "whole is integral and in i64 range"
    )]
    let whole_int = whole as i64;
    match int.cmp(&whole_int) {
        Ordering::Equal => 0.0_f64.partial_cmp(&(float - whole)),
        other => Some(other),
    }
}

/// Python's ordering of two numbers; `None` when unordered (NaN).
fn compare_numbers(left: Number, right: Number) -> Option<Ordering> {
    match (left, right) {
        (Number::Int(a), Number::Int(b)) => Some(a.cmp(&b)),
        (Number::Float(a), Number::Float(b)) => a.partial_cmp(&b),
        (Number::Int(a), Number::Float(b)) => compare_int_float(a, b),
        (Number::Float(a), Number::Int(b)) => compare_int_float(b, a).map(Ordering::reverse),
    }
}

fn holds(op: CompareOp, order: Option<Ordering>) -> bool {
    let Some(order) = order else {
        return matches!(op, CompareOp::Ne);
    };
    match op {
        CompareOp::Ge => order != Ordering::Less,
        CompareOp::Le => order != Ordering::Greater,
        CompareOp::Gt => order == Ordering::Greater,
        CompareOp::Lt => order == Ordering::Less,
        CompareOp::Eq => order == Ordering::Equal,
        CompareOp::Ne => order != Ordering::Equal,
    }
}

fn equals_choice(value: &Validated, choice: &Choice) -> bool {
    match (Number::of(value), choice) {
        (Some(number), Choice::Number(expected)) => {
            holds(CompareOp::Eq, compare_numbers(number, *expected))
        }
        (None, Choice::Str(expected)) => matches!(value, Validated::Str(text) if text == expected),
        _ => false,
    }
}

const UNCERTAIN_DATETIME: Defer = Defer("datetime Python may read differently");

fn digits_value(bytes: &[u8]) -> u32 {
    bytes
        .iter()
        .fold(0, |total, digit| total * 10 + u32::from(digit - b'0'))
}

/// The microseconds of an optional `.fraction` at `at` (1 to 6 digits; more
/// are left to Python) and where the text continues.
fn fraction_micros(bytes: &[u8], at: usize) -> Result<(u32, usize), Defer> {
    if bytes.get(at) != Some(&b'.') {
        return Ok((0, at));
    }
    let start = at + 1;
    let end = start
        + bytes[start..]
            .iter()
            .take_while(|byte| byte.is_ascii_digit())
            .count();
    let count = end - start;
    if count == 0 || count > 6 {
        return Err(UNCERTAIN_DATETIME);
    }
    let scale = 10_u32.pow(u32::try_from(6 - count).map_err(|_| UNCERTAIN_DATETIME)?);
    Ok((digits_value(&bytes[start..end]) * scale, end))
}

/// The UTC offset in seconds of `rest` (`Z` or `+hh:mm`/`-hh:mm` within a
/// day), `None` when there is none.
fn offset_seconds(rest: &[u8]) -> Result<Option<i32>, Defer> {
    let (sign, digits) = match rest {
        [] => return Ok(None),
        [b'Z'] => return Ok(Some(0)),
        [b'+', digits @ ..] => (1, digits),
        [b'-', digits @ ..] => (-1, digits),
        _ => return Err(UNCERTAIN_DATETIME),
    };
    let hours = digits_value(&digits[..2]);
    let minutes = digits_value(&digits[3..5]);
    if hours > 23 || minutes > 59 {
        return Err(UNCERTAIN_DATETIME);
    }
    let seconds = i32::try_from(hours * 3600 + minutes * 60).map_err(|_| UNCERTAIN_DATETIME)?;
    Ok(Some(sign * seconds))
}

/// An RFC 3339-shaped string (see [`is_rfc3339`]) as `datetime.fromisoformat`
/// reads it, for the inputs where every supported Python version agrees:
/// upper-case `T`/`Z`, real calendar dates from year 1, seconds below 60, at
/// most six fraction digits, offsets within a day. Anything else is
/// [`Defer`]red, never guessed.
fn native_datetime(text: &str) -> Result<(Validated, bool), Defer> {
    let bytes = text.as_bytes();
    if bytes.get(10) != Some(&b'T') {
        return Err(UNCERTAIN_DATETIME);
    }
    let field = |from: usize, to: usize| digits_value(&bytes[from..to]);
    let year = i32::try_from(field(0, 4)).map_err(|_| UNCERTAIN_DATETIME)?;
    let month = u8::try_from(field(5, 7)).map_err(|_| UNCERTAIN_DATETIME)?;
    let day = u8::try_from(field(8, 10)).map_err(|_| UNCERTAIN_DATETIME)?;
    let (hour, minute, second) = (field(11, 13), field(14, 16), field(17, 19));
    if year == 0 || hour > 23 || minute > 59 || second > 59 {
        return Err(UNCERTAIN_DATETIME);
    }
    let (micros, at) = fraction_micros(bytes, 19)?;
    let offset = offset_seconds(&bytes[at..])?;
    let month = Month::try_from(month).map_err(|_| UNCERTAIN_DATETIME)?;
    let date = Date::from_calendar_date(year, month, day).map_err(|_| UNCERTAIN_DATETIME)?;
    let clock = |value: u32| u8::try_from(value).map_err(|_| UNCERTAIN_DATETIME);
    let time = Time::from_hms_micro(clock(hour)?, clock(minute)?, clock(second)?, micros)
        .map_err(|_| UNCERTAIN_DATETIME)?;
    let local = PrimitiveDateTime::new(date, time);
    let Some(seconds) = offset else {
        return Ok((Validated::NaiveDatetime(local), true));
    };
    let offset = UtcOffset::from_whole_seconds(seconds).map_err(|_| UNCERTAIN_DATETIME)?;
    Ok((Validated::Datetime(local.assume_offset(offset)), false))
}

const BIG_INTEGER: Defer = Defer("integer outside i64");

impl<'v> View for JsonView<'v> {
    type Rules = NativeRules;
    type Data = &'v Json;
    type Str = &'v str;
    type Object = &'v JsonObject;
    type Key = &'v str;
    type List = &'v [Json];
    type Out = Validated;
    type ListOut = Vec<Validated>;
    type Record = Vec<(String, Validated)>;
    type Error = Defer;

    fn is_null(&self, data: &Self::Data) -> bool {
        matches!(data, Json::Null)
    }

    fn null(&self, _data: &Self::Data) -> Validated {
        Validated::Null
    }

    fn string(&self, data: &Self::Data) -> Option<&'v str> {
        match data {
            Json::Str(text) => Some(text.as_str()),
            _ => None,
        }
    }

    fn char_count(&self, text: &&'v str) -> Result<usize, Defer> {
        Ok(text.chars().count())
    }

    fn text<'s>(&self, text: &'s &'v str) -> Result<Cow<'s, str>, Defer> {
        Ok(Cow::Borrowed(text))
    }

    fn string_out(&self, text: &'v str) -> Validated {
        Validated::Str(text.to_owned())
    }

    fn integer(&self, data: &Self::Data) -> Result<Option<Validated>, Defer> {
        match data {
            Json::Int(int) => Ok(Some(Validated::Int(*int))),
            Json::BigInt(_) => Err(BIG_INTEGER),
            _ => Ok(None),
        }
    }

    fn number(&self, data: &Self::Data) -> Result<Option<Option<f64>>, Defer> {
        match data {
            // Round to nearest, ties to even: Python's int-to-float conversion.
            #[expect(
                clippy::cast_precision_loss,
                reason = "Python rounds int to float the same way"
            )]
            Json::Int(int) => Ok(Some(Some(*int as f64))),
            Json::Float(float) => Ok(Some(Some(*float))),
            Json::BigInt(_) => Err(BIG_INTEGER),
            _ => Ok(None),
        }
    }

    fn float(&self, value: f64) -> Validated {
        Validated::Float(value)
    }

    fn boolean(&self, data: &Self::Data) -> Option<Validated> {
        match data {
            Json::Bool(flag) => Some(Validated::Bool(*flag)),
            _ => None,
        }
    }

    fn uuid(&self, _data: &Self::Data, text: &str) -> Result<Validated, Defer> {
        Uuid::parse_str(text)
            .map(Validated::Uuid)
            .map_err(|_| Defer("uuid"))
    }

    fn datetime(&self, _data: &Self::Data, text: &str) -> Result<Option<(Validated, bool)>, Defer> {
        native_datetime(text).map(Some)
    }

    fn decimal_text(&self, data: &Self::Data) -> Result<Option<String>, Defer> {
        match data {
            Json::Str(text) => Ok(Some(text.clone())),
            Json::Int(int) => Ok(Some(int.to_string())),
            Json::BigInt(_) => Err(BIG_INTEGER),
            _ => Ok(None),
        }
    }

    fn wire(&self, wire: Wire, text: &str) -> Result<Validated, Defer> {
        Ok(match wire {
            Wire::Date(day) => Validated::Date(day),
            Wire::Time(clock) => Validated::Time(clock),
            Wire::Duration(span) => Validated::Duration(span),
            Wire::Decimal => Validated::Decimal(text.to_owned()),
            Wire::Bytes(raw) => Validated::Bytes(raw),
            Wire::Net(kind) => Validated::Net(kind, text.to_owned()),
        })
    }

    fn list(&self, data: &Self::Data) -> Option<&'v [Json]> {
        match data {
            Json::List(items) => Some(items.as_slice()),
            _ => None,
        }
    }

    fn list_len(&self, list: &&'v [Json]) -> usize {
        list.len()
    }

    fn items(list: &&'v [Json]) -> impl Iterator<Item = &'v Json> {
        list.iter()
    }

    fn new_list(&self) -> Vec<Validated> {
        Vec::new()
    }

    fn push(&self, out: &mut Vec<Validated>, value: Validated) -> Result<(), Defer> {
        out.push(value);
        Ok(())
    }

    fn close_list(&self, out: Vec<Validated>) -> Validated {
        Validated::List(out)
    }

    fn object(&self, data: &Self::Data) -> Option<&'v JsonObject> {
        match data {
            Json::Object(object) => Some(object),
            _ => None,
        }
    }

    fn non_string_keys(&self, _object: &&'v JsonObject) -> usize {
        0
    }

    fn string_keys(&self, object: &&'v JsonObject) -> Vec<&'v str> {
        object.keys().collect()
    }

    fn key_text<'k>(&self, key: &'k &'v str) -> Result<Cow<'k, str>, Defer> {
        Ok(Cow::Borrowed(key))
    }

    fn entries(&self, object: &&'v JsonObject) -> Result<Entries<&'v Json>, Defer> {
        Ok(object
            .iter()
            .map(|(key, value)| (Some(key.to_owned()), value))
            .collect())
    }

    fn contains(&self, object: &&'v JsonObject, key: &str) -> Result<bool, Defer> {
        Ok(object.contains(key))
    }

    fn get(&self, object: &&'v JsonObject, key: &str) -> Result<Option<&'v Json>, Defer> {
        Ok(object.get(key))
    }

    fn new_record(&self) -> Vec<(String, Validated)> {
        Vec::new()
    }

    fn set_field(
        &self,
        record: &mut Vec<(String, Validated)>,
        name: &str,
        value: Validated,
    ) -> Result<(), Defer> {
        record.push((name.to_owned(), value));
        Ok(())
    }

    fn close_record(
        &self,
        _class: &(),
        record: Vec<(String, Validated)>,
        _path: &str,
        _construct: bool,
    ) -> Result<Validated, Defer> {
        Ok(Validated::Object(record))
    }

    fn close_map(&self, record: Vec<(String, Validated)>) -> Validated {
        Validated::Object(record)
    }

    fn limit_fails(
        &self,
        value: &Validated,
        limit: &NativeLimit,
        op: CompareOp,
    ) -> Result<Option<String>, Defer> {
        let order = match (value, &limit.value) {
            (Validated::Decimal(text), LimitValue::Decimal(bound)) => {
                let number = DecimalNumber::parse(text).ok_or(Defer("decimal bound"))?;
                Some(number.cmp(bound))
            }
            (_, LimitValue::Number(bound)) => {
                let number = Number::of(value).ok_or(Defer("bound on a non-number"))?;
                compare_numbers(number, *bound)
            }
            _ => return Err(Defer("bound on a non-number")),
        };
        if holds(op, order) {
            return Ok(None);
        }
        Ok(Some(limit.shown.clone()))
    }

    fn multiple_fails(
        &self,
        value: &Validated,
        step: &NativeLimit,
    ) -> Result<Option<String>, Defer> {
        let multiple = match (value, &step.value) {
            (Validated::Int(int), LimitValue::Number(Number::Int(step))) => {
                int.checked_rem(*step) == Some(0)
            }
            (Validated::Decimal(text), LimitValue::Decimal(step)) => DecimalNumber::parse(text)
                .and_then(|number| number.is_multiple_of(step))
                .ok_or(Defer("multiple_of"))?,
            _ => return Err(Defer("multiple_of")),
        };
        Ok((!multiple).then(|| step.shown.clone()))
    }

    fn is_choice(&self, value: &Validated, choices: &NativeChoices) -> Result<bool, Defer> {
        Ok(choices
            .values
            .iter()
            .any(|choice| equals_choice(value, choice)))
    }

    fn choices_shown(&self, choices: &NativeChoices) -> String {
        choices.shown.clone()
    }

    fn member(&self, _class: &(), value: Validated) -> Result<Validated, Defer> {
        Ok(value)
    }

    fn collect(&self, _collection: Collection, _value: Validated) -> Result<Validated, Defer> {
        Err(Defer("collection"))
    }

    fn anything(&self, _data: &&'v Json) -> Result<Validated, Defer> {
        Err(Defer("any"))
    }

    fn pattern_shown(&self, pattern: &String) -> Result<String, Defer> {
        Ok(pattern.clone())
    }
}

/// One validation: the walk over the input, written once for every [`View`].
struct Run<'a, V: View> {
    view: V,
    definitions: &'a [Definition<V::Rules>],
    options: &'a Options,
    problems: Vec<Problem>,
    /// Plain unions being tried right now, and the failed container trials
    /// among them: the work of nested unions multiplies, so it is capped.
    trials: usize,
    wasted: usize,
}

/// Failed container variants one outermost union may burn before the whole
/// validation is refused.
const MAX_WASTED_TRIALS: usize = 1024;

fn is_composite<R: Rules>(kind: &Kind<R>) -> bool {
    matches!(
        kind,
        Kind::AnyOf(_)
            | Kind::List(_)
            | Kind::Tuple(_)
            | Kind::Map { .. }
            | Kind::Schema(_)
            | Kind::TaggedUnion { .. }
    )
}

impl<'a, V: View> Run<'a, V> {
    fn new(view: V, definitions: &'a [Definition<V::Rules>], options: &'a Options) -> Self {
        Run {
            view,
            definitions,
            options,
            problems: Vec::new(),
            trials: 0,
            wasted: 0,
        }
    }

    /// `(value, problems)` of the root schema at `root`: exactly one of them
    /// is meaningful.
    fn validate(mut self, root: usize, data: &V::Data) -> Outcome<V> {
        match self.schema(root, data, "", 0) {
            Ok(value) => Ok((value, self.problems)),
            Err(Abort::TooMany) => {
                let mut problems = self.problems;
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
            Err(Abort::View(err)) => Err(err),
        }
    }

    fn fail(&mut self, path: &str, code: &'static str, message: String) -> Checked<V> {
        self.problems.push((path.to_owned(), code, message));
        if self.problems.len() >= MAX_ERRORS {
            return Err(Abort::TooMany);
        }
        Ok(None)
    }

    fn check(
        &mut self,
        node: &Node<V::Rules>,
        data: &V::Data,
        path: &str,
        depth: usize,
    ) -> Checked<V> {
        if depth > MAX_DEPTH {
            return Err(Abort::Depth);
        }
        if self.view.is_null(data) {
            if node.nullable {
                return Ok(Some(self.view.null(data)));
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
            Kind::Date => self.day(data, path)?,
            Kind::Time => self.clock(data, path)?,
            Kind::Timedelta => self.duration(data, path)?,
            Kind::Decimal => self.decimal(node, data, path)?,
            Kind::Bytes => self.binary(node, data, path)?,
            Kind::Net(kind) => self.internet(*kind, data, path)?,
            Kind::List(item) => self.sequence(node, item, data, path, depth)?,
            Kind::Tuple(positions) => self.fixed(node, positions, data, path, depth)?,
            Kind::AnyOf(variants) => self.any_of(variants, data, path, depth)?,
            Kind::Any => Some(self.view.anything(data).map_err(Abort::View)?),
            Kind::Map { key, value } => self.mapping(node, key, value, data, path, depth)?,
            Kind::Schema(index) => self.schema(*index, data, path, depth)?,
            Kind::TaggedUnion {
                discriminator,
                discriminator_wire_name,
                variants,
            } => self.tagged_union(
                discriminator,
                discriminator_wire_name,
                variants,
                data,
                path,
                depth,
            )?,
        };
        self.settle(node, checked, path)
    }

    /// The checked value after `one_of` and, for an `Enum` field, as its member.
    fn settle(&mut self, node: &Node<V::Rules>, checked: Option<V::Out>, path: &str) -> Checked<V> {
        let checked = match (&node.one_of, checked) {
            (Some(choices), Some(value)) => self.one_of(choices, value, path)?,
            (_, other) => other,
        };
        let checked = match (&node.member, checked) {
            (Some(class), Some(value)) => self.member(class, value)?,
            (_, other) => other,
        };
        match (node.collection, checked) {
            (Some(collection), Some(value)) => self.collect(collection, value),
            (_, other) => Ok(other),
        }
    }

    /// The checked array as a set, frozenset or tuple.
    fn collect(&mut self, collection: Collection, value: V::Out) -> Checked<V> {
        self.view
            .collect(collection, value)
            .map(Some)
            .map_err(Abort::View)
    }

    /// The member of the field's `Enum` that the checked value names.
    fn member(&mut self, class: &<V::Rules as Rules>::Member, value: V::Out) -> Checked<V> {
        self.view
            .member(class, value)
            .map(Some)
            .map_err(Abort::View)
    }

    /// The value must equal one of the declared choices (strict types were
    /// already enforced by the kind, so `True` never passes as an integer).
    fn one_of(
        &mut self,
        choices: &<V::Rules as Rules>::Choices,
        value: V::Out,
        path: &str,
    ) -> Checked<V> {
        if self.view.is_choice(&value, choices).map_err(Abort::View)? {
            return Ok(Some(value));
        }
        let shown = self.view.choices_shown(choices);
        self.fail(path, "one_of", format!("must be one of {shown}"))
    }

    /// The `max_length` of `node`, or its wire-input bound on wire input.
    fn most(&self, node: &Node<V::Rules>) -> Option<i64> {
        node.max_length.or(if self.options.input_bounds {
            node.input_max_length
        } else {
            None
        })
    }

    fn length(
        &mut self,
        node: &Node<V::Rules>,
        size: usize,
        path: &str,
        unit: &str,
    ) -> Result<bool, Abort<V::Error>> {
        let size = i64::try_from(size).unwrap_or(i64::MAX);
        if let Some(least) = node.min_length {
            if size < least {
                let unit = if least == 1 {
                    unit.to_owned()
                } else {
                    format!("{unit}s")
                };
                self.fail(path, "min_length", format!("at least {least} {unit}"))?;
                return Ok(false);
            }
        }
        if let Some(most) = self.most(node) {
            if size > most {
                let unit = if most == 1 {
                    unit.to_owned()
                } else {
                    format!("{unit}s")
                };
                self.fail(path, "max_length", format!("at most {most} {unit}"))?;
                return Ok(false);
            }
        }
        Ok(true)
    }

    fn text(&mut self, node: &Node<V::Rules>, data: &V::Data, path: &str) -> Checked<V> {
        let Some(text) = self.view.string(data) else {
            return self.fail(path, "type", "expected a string".to_owned());
        };
        let size = self.view.char_count(&text).map_err(Abort::View)?;
        if !self.length(node, size, path, "character")? {
            return Ok(None);
        }
        if let Some((source, regex)) = &node.pattern {
            let matched = self
                .view
                .text(&text)
                .map(|s| regex.is_match(&s))
                .unwrap_or(false);
            if !matched {
                let shown = self.view.pattern_shown(source).map_err(Abort::View)?;
                return self.fail(path, "pattern", format!("must match {shown}"));
            }
        }
        Ok(Some(self.view.string_out(text)))
    }

    fn bounds(&mut self, node: &Node<V::Rules>, value: V::Out, path: &str) -> Checked<V> {
        let tests = [
            ("ge", "greater than or equal to", CompareOp::Ge, &node.ge),
            ("le", "less than or equal to", CompareOp::Le, &node.le),
            ("gt", "greater than", CompareOp::Gt, &node.gt),
            ("lt", "less than", CompareOp::Lt, &node.lt),
        ];
        for (name, words, op, limit) in tests {
            if let Some(limit) = limit {
                let failed = self
                    .view
                    .limit_fails(&value, limit, op)
                    .map_err(Abort::View)?;
                if let Some(shown) = failed {
                    return self.fail(path, name, format!("must be {words} {shown}"));
                }
            }
        }
        if let Some(step) = &node.multiple_of {
            let failed = self
                .view
                .multiple_fails(&value, step)
                .map_err(Abort::View)?;
            if let Some(shown) = failed {
                return self.fail(
                    path,
                    "multiple_of",
                    format!("must be a multiple of {shown}"),
                );
            }
        }
        Ok(Some(value))
    }

    fn integer(&mut self, node: &Node<V::Rules>, data: &V::Data, path: &str) -> Checked<V> {
        let Some(value) = self.view.integer(data).map_err(Abort::View)? else {
            return self.fail(path, "type", "expected an integer".to_owned());
        };
        self.bounds(node, value, path)
    }

    fn number(&mut self, node: &Node<V::Rules>, data: &V::Data, path: &str) -> Checked<V> {
        let Some(number) = self.view.number(data).map_err(Abort::View)? else {
            return self.fail(path, "type", "expected a number".to_owned());
        };
        let Some(value) = number else {
            return self.fail(path, "finite", "number is out of range".to_owned());
        };
        if !value.is_finite() {
            return self.fail(path, "finite", "number must be finite".to_owned());
        }
        let converted = self.view.float(value);
        self.bounds(node, converted, path)
    }

    fn boolean(&mut self, data: &V::Data, path: &str) -> Checked<V> {
        let Some(value) = self.view.boolean(data) else {
            return self.fail(path, "type", "expected a boolean".to_owned());
        };
        Ok(Some(value))
    }

    /// The content of a string input, `None` for anything else or a string
    /// that is not valid Unicode.
    fn plain_text(&self, data: &V::Data) -> Option<String> {
        let text = self.view.string(data)?;
        self.view.text(&text).ok().map(Cow::into_owned)
    }

    fn uuid(&mut self, data: &V::Data, path: &str) -> Checked<V> {
        let text = self.plain_text(data).filter(|text| is_canonical_uuid(text));
        let Some(text) = text else {
            return self.fail(path, "uuid", "expected a canonical UUID string".to_owned());
        };
        Ok(Some(self.view.uuid(data, &text).map_err(Abort::View)?))
    }

    fn moment(&mut self, data: &V::Data, path: &str) -> Checked<V> {
        let message = "expected an RFC 3339 datetime string";
        let text = self.plain_text(data).filter(|text| is_rfc3339(text));
        let Some(text) = text else {
            return self.fail(path, "datetime", message.to_owned());
        };
        let Some((parsed, naive)) = self.view.datetime(data, &text).map_err(Abort::View)? else {
            return self.fail(path, "datetime", message.to_owned());
        };
        if naive && !self.options.allow_naive_datetime {
            return self.fail(
                path,
                "datetime_tz",
                "datetime needs a UTC offset".to_owned(),
            );
        }
        Ok(Some(parsed))
    }

    fn day(&mut self, data: &V::Data, path: &str) -> Checked<V> {
        let text = self.plain_text(data);
        let parsed = text
            .as_deref()
            .and_then(|text| scalars::parse_date(text).map(|day| (day, text)));
        let Some((day, text)) = parsed else {
            let message = "expected an ISO 8601 date string";
            return self.fail(path, "date", message.to_owned());
        };
        Ok(Some(
            self.view.wire(Wire::Date(day), text).map_err(Abort::View)?,
        ))
    }

    fn clock(&mut self, data: &V::Data, path: &str) -> Checked<V> {
        let text = self.plain_text(data);
        let parsed = text
            .as_deref()
            .and_then(|text| scalars::parse_time(text).map(|clock| (clock, text)));
        let Some((clock, text)) = parsed else {
            let message = "expected an ISO 8601 time string";
            return self.fail(path, "time", message.to_owned());
        };
        if clock.offset.is_none() && !self.options.allow_naive_datetime {
            return self.fail(path, "time_tz", "time needs a UTC offset".to_owned());
        }
        Ok(Some(
            self.view
                .wire(Wire::Time(clock), text)
                .map_err(Abort::View)?,
        ))
    }

    fn internet(&mut self, kind: NetKind, data: &V::Data, path: &str) -> Checked<V> {
        let text = self.plain_text(data).filter(|text| kind.accepts(text));
        let Some(text) = text else {
            return self.fail(path, kind.code(), kind.message().to_owned());
        };
        Ok(Some(
            self.view
                .wire(Wire::Net(kind), &text)
                .map_err(Abort::View)?,
        ))
    }

    fn duration(&mut self, data: &V::Data, path: &str) -> Checked<V> {
        let text = self.plain_text(data);
        let parsed = text
            .as_deref()
            .map(|text| scalars::parse_duration(text).map(|span| (span, text)));
        match parsed {
            Some(Ok((span, text))) => Ok(Some(
                self.view
                    .wire(Wire::Duration(span), text)
                    .map_err(Abort::View)?,
            )),
            Some(Err(DurationError::Range)) => self.fail(
                path,
                "duration_range",
                "duration is out of range".to_owned(),
            ),
            _ => {
                let message = "expected an ISO 8601 duration string";
                self.fail(path, "duration", message.to_owned())
            }
        }
    }

    fn decimal(&mut self, node: &Node<V::Rules>, data: &V::Data, path: &str) -> Checked<V> {
        let text = self.view.decimal_text(data).map_err(Abort::View)?;
        let parsed = text
            .as_deref()
            .and_then(|text| scalars::decimal_shape(text).map(|shape| (shape, text)));
        let Some((shape, text)) = parsed else {
            let message = "expected a decimal string or an integer";
            return self.fail(path, "decimal", message.to_owned());
        };
        if !self.decimal_digits(node, shape, path)? {
            return Ok(None);
        }
        let value = self.view.wire(Wire::Decimal, text).map_err(Abort::View)?;
        self.bounds(node, value, path)
    }

    /// Whether a decimal has no more digits, decimal places and whole digits
    /// than the node allows; reports the first limit it breaks at `path`.
    fn decimal_digits(
        &mut self,
        node: &Node<V::Rules>,
        shape: scalars::DecimalShape,
        path: &str,
    ) -> Result<bool, Abort<V::Error>> {
        let most = node.max_digits.unwrap_or(MAX_DECIMAL_DIGITS);
        let whole = node.decimal_places.map(|places| most - places);
        let tests = [
            (shape.digits, Some(most), "max_digits", "digit", ""),
            (
                shape.places,
                node.decimal_places,
                "decimal_places",
                "decimal place",
                "",
            ),
            (
                shape.digits.saturating_sub(shape.places),
                whole,
                "max_whole_digits",
                "digit",
                " before the decimal point",
            ),
        ];
        for (count, limit, code, unit, after) in tests {
            let count = i64::try_from(count).unwrap_or(i64::MAX);
            let Some(limit) = limit.filter(|limit| count > *limit) else {
                continue;
            };
            let plural = if limit == 1 { "" } else { "s" };
            let message = format!("at most {limit} {unit}{plural}{after}");
            self.fail(path, code, message)?;
            return Ok(false);
        }
        Ok(true)
    }

    fn binary(&mut self, node: &Node<V::Rules>, data: &V::Data, path: &str) -> Checked<V> {
        let text = self.plain_text(data);
        let decoded = text
            .as_deref()
            .and_then(|text| scalars::decode_base64(text).map(|raw| (raw, text)));
        let Some((raw, text)) = decoded else {
            return self.fail(path, "bytes", "expected a base64 string".to_owned());
        };
        if !self.length(node, raw.len(), path, "byte")? {
            return Ok(None);
        }
        Ok(Some(
            self.view
                .wire(Wire::Bytes(raw), text)
                .map_err(Abort::View)?,
        ))
    }

    fn sequence(
        &mut self,
        node: &Node<V::Rules>,
        item: &Node<V::Rules>,
        data: &V::Data,
        path: &str,
        depth: usize,
    ) -> Checked<V> {
        let Some(list) = self.view.list(data) else {
            return self.fail(path, "type", "expected an array".to_owned());
        };
        if !self.length(node, self.view.list_len(&list), path, "item")? {
            return Ok(None);
        }
        let before = self.problems.len();
        let mut out = self.view.new_list();
        for (index, element) in V::items(&list).enumerate() {
            let found = self.check(item, &element, &format!("{path}[{index}]"), depth + 1)?;
            if let Some(value) = found {
                self.view.push(&mut out, value).map_err(Abort::View)?;
            }
        }
        Ok(if self.problems.len() > before {
            None
        } else {
            Some(self.view.close_list(out))
        })
    }

    /// A `tuple[X, Y]` field: an array of exactly as many items as positions,
    /// each checked at `path[i]` by its position's node, the result a tuple.
    /// A plain union: each variant is tried on the same data in declared order;
    /// the trial's problems are dropped, and a value no variant takes is one
    /// `union` problem.
    fn any_of(
        &mut self,
        variants: &[Node<V::Rules>],
        data: &V::Data,
        path: &str,
        depth: usize,
    ) -> Checked<V> {
        if self.trials == 0 {
            self.wasted = 0;
        }
        self.trials += 1;
        let found = self.try_variants(variants, data, path, depth);
        self.trials -= 1;
        found
    }

    fn try_variants(
        &mut self,
        variants: &[Node<V::Rules>],
        data: &V::Data,
        path: &str,
        depth: usize,
    ) -> Checked<V> {
        let before = self.problems.len();
        for variant in variants {
            if self.wasted > MAX_WASTED_TRIALS {
                return Err(Abort::TooMany);
            }
            let tried = match self.check(variant, data, path, depth + 1) {
                Err(Abort::TooMany) if self.wasted <= MAX_WASTED_TRIALS => None,
                other => other?,
            };
            self.problems.truncate(before);
            if tried.is_some() {
                return Ok(tried);
            }
            if self.trials > 1 && is_composite(&variant.kind) {
                self.wasted += 1;
            }
        }
        self.fail(path, "union", "does not match any allowed type".to_owned())
    }

    fn fixed(
        &mut self,
        node: &Node<V::Rules>,
        positions: &[Node<V::Rules>],
        data: &V::Data,
        path: &str,
        depth: usize,
    ) -> Checked<V> {
        let Some(list) = self.view.list(data) else {
            return self.fail(path, "type", "expected an array".to_owned());
        };
        if !self.length(node, self.view.list_len(&list), path, "item")? {
            return Ok(None);
        }
        let before = self.problems.len();
        let mut out = self.view.new_list();
        for ((index, element), position) in V::items(&list).enumerate().zip(positions) {
            let found = self.check(position, &element, &format!("{path}[{index}]"), depth + 1)?;
            if let Some(value) = found {
                self.view.push(&mut out, value).map_err(Abort::View)?;
            }
        }
        if self.problems.len() > before {
            return Ok(None);
        }
        let closed = self.view.close_list(out);
        self.collect(Collection::Tuple, closed)
    }

    /// A `dict[str, X]` field: an object of string keys, its entry count
    /// within the node's limits, each key within the key node's, each value
    /// checked at `path.key` (the key shortened as unknown fields are).
    fn mapping(
        &mut self,
        node: &Node<V::Rules>,
        key: &Node<V::Rules>,
        value: &Node<V::Rules>,
        data: &V::Data,
        path: &str,
        depth: usize,
    ) -> Checked<V> {
        let Some(object) = self.view.object(data) else {
            return self.fail(path, "type", "expected an object".to_owned());
        };
        let before = self.problems.len();
        self.validate_object_key_types(&object, path)?;
        if self.problems.len() > before {
            return Ok(None);
        }
        let entries = self.view.entries(&object).map_err(Abort::View)?;
        if !self.length(node, entries.len(), path, "item")? {
            return Ok(None);
        }
        let mut out = self.view.new_record();
        for (name, raw) in entries {
            let Some(name) = name else {
                let message = "object keys must be valid Unicode text".to_owned();
                self.fail(path, "type", message)?;
                continue;
            };
            let shown: String = name.chars().take(MAX_KEY_ECHO).collect();
            let here = join(path, &shown);
            if !self.map_key(key, &name, &here)? {
                continue;
            }
            if let Some(found) = self.check(value, &raw, &here, depth + 1)? {
                self.view
                    .set_field(&mut out, &name, found)
                    .map_err(Abort::View)?;
            }
        }
        Ok(if self.problems.len() > before {
            None
        } else {
            Some(self.view.close_map(out))
        })
    }

    /// Whether a `dict` key satisfies the key node's length and pattern;
    /// reports the first limit it breaks at `path` otherwise.
    fn map_key(
        &mut self,
        key: &Node<V::Rules>,
        name: &str,
        path: &str,
    ) -> Result<bool, Abort<V::Error>> {
        let size = i64::try_from(name.chars().count()).unwrap_or(i64::MAX);
        let plural = |bound: i64| if bound == 1 { "" } else { "s" };
        if let Some(least) = key.min_length.filter(|least| size < *least) {
            let message = format!("key must have at least {least} character{}", plural(least));
            self.fail(path, "key_min_length", message)?;
            return Ok(false);
        }
        if let Some(most) = self.most(key).filter(|most| size > *most) {
            let message = format!("key must have at most {most} character{}", plural(most));
            self.fail(path, "key_max_length", message)?;
            return Ok(false);
        }
        if let Some((source, regex)) = &key.pattern {
            if !regex.is_match(name) {
                let shown = self.view.pattern_shown(source).map_err(Abort::View)?;
                self.fail(path, "key_pattern", format!("key must match {shown}"))?;
                return Ok(false);
            }
        }
        Ok(true)
    }

    fn schema(&mut self, index: usize, data: &V::Data, path: &str, depth: usize) -> Checked<V> {
        let Some(object) = self.view.object(data) else {
            return self.fail(path, "type", "expected an object".to_owned());
        };
        let definitions = self.definitions;
        let definition = &definitions[index];
        let before = self.problems.len();
        let use_aliases = self.options.use_aliases;
        let known: HashSet<&str> = definition
            .fields
            .iter()
            .map(|field| {
                if use_aliases {
                    field.wire_name.as_str()
                } else {
                    field.name.as_str()
                }
            })
            .collect();
        self.validate_object_key_types(&object, path)?;
        if self.problems.len() > before {
            return Ok(None);
        }
        let duplicate_names = if use_aliases {
            self.duplicate_python_names(definition, &object, path)?
        } else {
            HashSet::new()
        };
        if definition.forbid_extra {
            self.reject_unknown_fields(&object, &known, &duplicate_names, path)?;
        }
        let values = self.validate_fields(definition, &object, &duplicate_names, path, depth)?;
        if self.problems.len() > before {
            return Ok(None);
        }
        let construct = self.options.construct;
        let built = self
            .view
            .close_record(&definition.class, values, path, construct)
            .map_err(Abort::View)?;
        Ok(Some(built))
    }

    fn tagged_union(
        &mut self,
        discriminator: &str,
        wire_name: &str,
        variants: &[(String, usize)],
        data: &V::Data,
        path: &str,
        depth: usize,
    ) -> Checked<V> {
        let Some(object) = self.view.object(data) else {
            return self.fail(path, "type", "expected an object".to_owned());
        };
        let before = self.problems.len();
        self.validate_object_key_types(&object, path)?;
        if self.problems.len() > before {
            return Ok(None);
        }
        let use_aliases = self.options.use_aliases;
        let field_name = if use_aliases {
            wire_name
        } else {
            discriminator
        };
        let field_path = join(path, field_name);
        if use_aliases
            && wire_name != discriminator
            && self.both_present(&object, discriminator, wire_name)?
        {
            self.fail(
                &field_path,
                "duplicate_field",
                "field was supplied by both its Python name and alias".to_owned(),
            )?;
            return Ok(None);
        }
        let Some(tag) = self.discriminator_tag(&object, field_name, &field_path)? else {
            return Ok(None);
        };
        let Some((_, index)) = variants.iter().find(|(candidate, _)| *candidate == tag) else {
            self.fail(
                &field_path,
                "discriminator_unknown",
                "unknown discriminator value".to_owned(),
            )?;
            return Ok(None);
        };
        self.schema(*index, data, path, depth)
    }

    /// The discriminator's string value, or `None` after reporting why there
    /// is none.
    fn discriminator_tag(
        &mut self,
        object: &V::Object,
        field_name: &str,
        field_path: &str,
    ) -> Result<Option<String>, Abort<V::Error>> {
        let Some(raw_tag) = self.view.get(object, field_name).map_err(Abort::View)? else {
            self.fail(
                field_path,
                "discriminator_required",
                "discriminator field is required".to_owned(),
            )?;
            return Ok(None);
        };
        let Some(tag) = self.view.string(&raw_tag) else {
            self.fail(
                field_path,
                "discriminator_type",
                "discriminator must be a string".to_owned(),
            )?;
            return Ok(None);
        };
        Ok(Some(
            self.view.text(&tag).map_err(Abort::View)?.into_owned(),
        ))
    }

    fn both_present(
        &self,
        object: &V::Object,
        name: &str,
        wire_name: &str,
    ) -> Result<bool, Abort<V::Error>> {
        Ok(self.view.contains(object, name).map_err(Abort::View)?
            && self.view.contains(object, wire_name).map_err(Abort::View)?)
    }

    fn duplicate_python_names(
        &mut self,
        definition: &Definition<V::Rules>,
        object: &V::Object,
        path: &str,
    ) -> Result<HashSet<String>, Abort<V::Error>> {
        let mut duplicates = HashSet::new();
        for field in &definition.fields {
            if field.name != field.wire_name
                && self.both_present(object, &field.name, &field.wire_name)?
            {
                duplicates.insert(field.name.clone());
                self.fail(
                    &join(path, &field.wire_name),
                    "duplicate_field",
                    "field was supplied by both its Python name and alias".to_owned(),
                )?;
            }
        }
        Ok(duplicates)
    }

    fn reject_unknown_fields(
        &mut self,
        object: &V::Object,
        known: &HashSet<&str>,
        duplicate_names: &HashSet<String>,
        path: &str,
    ) -> Result<(), Abort<V::Error>> {
        for key in self.view.string_keys(object) {
            let name = self.view.key_text(&key).map_err(Abort::View)?.into_owned();
            let known_wire_name = known.contains(name.as_str());
            let duplicate_python_name = duplicate_names.contains(&name);
            if !known_wire_name && !duplicate_python_name {
                let shown: String = name.chars().take(MAX_KEY_ECHO).collect();
                self.fail(
                    &join(path, &shown),
                    "extra_forbidden",
                    "unknown field".to_owned(),
                )?;
            }
        }
        Ok(())
    }

    fn validate_object_key_types(
        &mut self,
        object: &V::Object,
        path: &str,
    ) -> Result<(), Abort<V::Error>> {
        for _ in 0..self.view.non_string_keys(object) {
            self.fail(path, "type", "object keys must be strings".to_owned())?;
        }
        Ok(())
    }

    fn validate_fields(
        &mut self,
        definition: &Definition<V::Rules>,
        object: &V::Object,
        duplicate_names: &HashSet<String>,
        path: &str,
        depth: usize,
    ) -> Result<V::Record, Abort<V::Error>> {
        let mut values = self.view.new_record();
        for field in &definition.fields {
            if path.is_empty() && self.options.skip_fields.contains(&field.name) {
                continue;
            }
            if duplicate_names.contains(&field.name) {
                continue;
            }
            let key = if self.options.use_aliases {
                &field.wire_name
            } else {
                &field.name
            };
            let here = join(path, key);
            match self.view.get(object, key).map_err(Abort::View)? {
                Some(raw) => {
                    if let Some(value) = self.check(&field.node, &raw, &here, depth + 1)? {
                        self.view
                            .set_field(&mut values, &field.name, value)
                            .map_err(Abort::View)?;
                    }
                }
                None if field.required => {
                    self.fail(&here, "required", "field is required".to_owned())?;
                }
                None => {}
            }
        }
        Ok(values)
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

// The GIL-free entry points have no caller in the extension yet: the native
// CRUD executor uses them. Allowing them keeps everything they reach checked.
#[cfg_attr(
    not(test),
    allow(dead_code, reason = "consumed by the native CRUD executor")
)]
impl Compiled {
    /// The GIL-free copy of this schema; `None` when a constraint has no
    /// exact plain-Rust equivalent.
    pub(crate) fn native(&self) -> Option<&NativeSchema> {
        self.native.as_deref()
    }

    pub(crate) fn native_shared(&self) -> Option<Arc<NativeSchema>> {
        self.native.as_ref().map(Arc::clone)
    }
}

// The GIL-free entry points have no caller in the extension yet: the native
// CRUD executor uses them. Allowing them keeps everything they reach checked.
#[cfg_attr(
    not(test),
    allow(dead_code, reason = "consumed by the native CRUD executor")
)]
impl NativeSchema {
    pub(crate) fn field_names(&self) -> Vec<String> {
        self.definitions[self.root]
            .fields
            .iter()
            .map(|field| field.name.clone())
            .collect()
    }

    /// Validate a decoded document without the GIL. `Ok` carries exactly
    /// the problems the Python path reports for the same input, or the
    /// value it would return with `construct=False`; `Err(Defer)` when that
    /// cannot be guaranteed. `options.construct` has no effect here.
    pub(crate) fn validate(
        &self,
        data: &Json,
        options: &Options,
    ) -> Result<(Option<Validated>, Vec<Problem>), Defer> {
        let view = JsonView { input: PhantomData };
        Run::new(view, &self.definitions, options).validate(self.root, &data)
    }
}

#[pymethods]
impl Compiled {
    /// The tree of a text source (form fields, query pairs, XML) with its text
    /// leaves converted to the scalar types this schema declares, ready for the
    /// strict `validate`. What does not convert is left for `validate` to refuse.
    #[pyo3(signature = (data, use_aliases=true))]
    fn coerce_text<'py>(
        &self,
        py: Python<'py>,
        data: &Bound<'py, PyAny>,
        use_aliases: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        coerce::coerce_object(py, &self.definitions, self.root, data, use_aliases, 0)
    }

    /// `(instance, problems)`: exactly one of them is meaningful. Problems are
    /// `(path, code, message)` tuples in the order the checks found them.
    /// `input_bounds` (default: `construct`) applies the wire-input bounds of
    /// dict fields; construction and model conversion leave it off.
    #[pyo3(signature = (data, construct=true, use_aliases=true, allow_naive_datetime=false, skip_fields=Vec::new(), input_bounds=None))]
    #[expect(
        clippy::too_many_arguments,
        reason = "the keyword arguments of the Python method"
    )]
    fn validate<'py>(
        &self,
        py: Python<'py>,
        data: &Bound<'py, PyAny>,
        construct: bool,
        use_aliases: bool,
        allow_naive_datetime: bool,
        skip_fields: Vec<String>,
        input_bounds: Option<bool>,
    ) -> PyResult<(Option<Bound<'py, PyAny>>, Vec<Problem>)> {
        let options = Options {
            construct,
            use_aliases,
            allow_naive_datetime,
            skip_fields: skip_fields.into_iter().collect(),
            input_bounds: input_bounds.unwrap_or(construct),
        };
        let view = PyView {
            py,
            uuid: &self.uuid,
            from_iso: &self.from_iso,
        };
        Run::new(view, &self.definitions, &options).validate(self.root, data)
    }
}

/// `object.__new__(class)` filled from `record`, as the generated constructor
/// would, without the Python round trip.
fn build_direct<'py>(
    py: Python<'py>,
    class: &Bound<'py, PyAny>,
    direct: &Direct,
    record: &Bound<'py, PyDict>,
) -> PyResult<Bound<'py, PyAny>> {
    let instance = py.get_type::<PyAny>().call_method1("__new__", (class,))?;
    let attributes = instance.getattr("__dict__")?;
    let attributes = attributes.cast::<PyDict>()?;
    attributes.update(record.as_mapping())?;
    if let Some(supplied) = &direct.supplied {
        attributes.set_item("__hinoki_supplied_fields__", supplied)?;
    }
    Ok(instance)
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

/// The Python path and the GIL-free path over the same inputs: the same
/// problems (paths, codes, messages, order) and the same accepted values, or
/// an explicit deferral by the GIL-free path.
#[cfg(test)]
mod differential {
    use std::ffi::CString;

    use pyo3::types::PyDict;

    use super::*;
    use crate::request::{decode_json, decode_json_document, json_to_python, Json, JsonObject};

    const SETUP: &str = r#"
import datetime as dt
import decimal
import uuid


class Made:
    @classmethod
    def __hinoki_construct_validated__(cls, values, path):
        return (cls.__name__, values, path)


class Text(str):
    pass


def node(kind, nullable=False, item=None, index=None, **constraints):
    return (kind, nullable, constraints, item, index)
"#;

    const SCALARS: &str = r#"[(Made, [
        ("name", "name", node("str", min_length=1, max_length=5, pattern="[a-zé']+"), True),
        ("count", "count", node("int", ge=0, le=10), True),
        ("ratio", "ratio", node("float", gt=0, lt=1.5), False),
        ("flag", "flag", node("bool"), False),
        ("key", "key", node("uuid"), False),
        ("at", "at", node("datetime", nullable=True), False),
        ("note", "note", node("str", nullable=True, max_length=1), False),
        ("kind", "kind", node("str", one_of=("a", "b'c", 'é"x')), False),
        ("level", "level", node("int", one_of=(1, 2.0, True, 7)), False),
        ("yes", "yes", node("bool", one_of=(1,)), False),
        ("weight", "weight", node("float", ge=9007199254740993, one_of=(9007199254740994.0, 1e300, 3)), False),
        ("zero", "zero", node("float", one_of=(0.5, False)), False),
        ("small", "small", node("int", gt=-0.0, le=1.5), False),
        ("nan", "nan", node("float", ge=float("nan")), False),
        ("edge", "edge", node("int", ge=-9223372036854775808.0, le=9223372036854775807.0), False),
        ("wide", "wide", node("int", gt=-1e19, lt=1e19), False),
        ("digit", "digit", node("int", one_of=tuple(range(12))), False),
        ("inf", "inf", node("float", le=float("inf"), gt=-float("inf")), False),
    ], True)]"#;

    const ALIASES: &str = r#"[(Made, [
        ("user_name", "userName", node("str"), True),
        ("age", "age", node("int", nullable=True), False),
    ], True)]"#;

    const NESTED: &str = r#"[
        (Made, [
            ("items", "items", node("list", item=node("schema", index=1), min_length=1, max_length=3), True),
            ("child", "child", node("schema", nullable=True, index=1), False),
            ("tags", "tags", node("list", nullable=True, item=node("str", max_length=2)), False),
            ("grid", "grid", node("list", item=node("list", item=node("int", ge=0), max_length=2)), False),
        ], False),
        (Made, [
            ("id", "id", node("int"), True),
            ("label", "label", node("str", min_length=2), False),
        ], True),
    ]"#;

    const UNION: &str = r#"[
        (Made, [
            ("pet", "pet", node("union", discriminator="kind", discriminator_wire_name="type",
                                variants=[("cat", 1), ("dog", 2)]), True),
            ("pets", "pets", node("list", item=node("union", nullable=True, discriminator="kind",
                                  discriminator_wire_name="kind", variants=[("cat", 1), ("dog", 2)])), False),
        ], True),
        (Made, [("kind", "type", node("str"), True), ("lives", "lives", node("int", le=9), True)], True),
        (Made, [("kind", "type", node("str"), True), ("good", "good", node("bool"), False)], False),
    ]"#;

    const MAPPING: &str = r#"[
        (Made, [
            ("labels", "labels", node("dict", item=node("str", max_length=3),
                                      key=node("str", max_length=4), max_length=3), True),
            ("points", "points", node("dict", item=node("schema", index=1),
                                      key=node("str", min_length=2, max_length=256, pattern="[a-z]+")), False),
            ("grid", "grid", node("dict", nullable=True, item=node("list", nullable=True, item=node("int")),
                                  key=node("str", max_length=256), min_length=1), False),
        ], True),
        (Made, [("x", "x", node("int"), True), ("y", "y", node("int", ge=0), False)], True),
    ]"#;

    const CHAIN: &str = r#"[(Made, [
        ("next", "next", node("schema", nullable=True, index=0), False),
        ("n", "n", node("int"), False),
    ], False)]"#;

    fn compile(py: Python<'_>, definitions: &str) -> Compiled {
        let namespace = PyDict::new(py);
        py.run(&CString::new(SETUP).unwrap(), Some(&namespace), None)
            .unwrap();
        let table = py
            .eval(&CString::new(definitions).unwrap(), Some(&namespace), None)
            .unwrap();
        let uuid = py.import("uuid").unwrap().getattr("UUID").unwrap();
        let from_iso = py
            .import("datetime")
            .unwrap()
            .getattr("datetime")
            .unwrap()
            .getattr("fromisoformat")
            .unwrap();
        compile_schemas(
            py,
            table.extract().unwrap(),
            0,
            uuid.unbind(),
            from_iso.unbind(),
            py.None(),
            None,
        )
        .unwrap()
    }

    fn options(use_aliases: bool, allow_naive_datetime: bool, skip: &[&str]) -> Options {
        Options {
            construct: false,
            use_aliases,
            allow_naive_datetime,
            skip_fields: skip.iter().map(|name| (*name).to_owned()).collect(),
            input_bounds: false,
        }
    }

    /// The Python object the Python path returns for the same accepted value.
    fn python_value<'py>(py: Python<'py>, value: &Validated) -> Bound<'py, PyAny> {
        let datetime = py.import("datetime").unwrap();
        let moment = |local: PrimitiveDateTime| {
            (
                local.year(),
                u8::from(local.month()),
                local.day(),
                local.hour(),
                local.minute(),
                local.second(),
                local.microsecond(),
            )
        };
        match value {
            Validated::Null => py.None().into_bound(py),
            Validated::Bool(flag) => PyBool::new(py, *flag).to_owned().into_any(),
            Validated::Int(int) => PyInt::new(py, *int).into_any(),
            Validated::Float(float) => PyFloat::new(py, *float).into_any(),
            Validated::Str(text) => PyString::new(py, text).into_any(),
            Validated::Uuid(uuid) => py
                .import("uuid")
                .unwrap()
                .getattr("UUID")
                .unwrap()
                .call1((uuid.hyphenated().to_string(),))
                .unwrap(),
            Validated::NaiveDatetime(local) => datetime
                .getattr("datetime")
                .unwrap()
                .call1(moment(*local))
                .unwrap(),
            Validated::Datetime(aware) => {
                let delta = datetime
                    .getattr("timedelta")
                    .unwrap()
                    .call1((0, aware.offset().whole_seconds()))
                    .unwrap();
                let zone = datetime
                    .getattr("timezone")
                    .unwrap()
                    .call1((delta,))
                    .unwrap();
                let local = PrimitiveDateTime::new(aware.date(), aware.time());
                let keywords = PyDict::new(py);
                keywords.set_item("tzinfo", zone).unwrap();
                datetime
                    .getattr("datetime")
                    .unwrap()
                    .call(moment(local), Some(&keywords))
                    .unwrap()
            }
            Validated::Date(_)
            | Validated::Time(_)
            | Validated::Duration(_)
            | Validated::Decimal(_)
            | Validated::Net(..)
            | Validated::Bytes(_) => python_wire_value(py, value),
            Validated::List(items) => {
                let list = PyList::empty(py);
                for item in items {
                    list.append(python_value(py, item)).unwrap();
                }
                list.into_any()
            }
            Validated::Object(fields) => {
                let dict = PyDict::new(py);
                for (name, item) in fields {
                    dict.set_item(name, python_value(py, item)).unwrap();
                }
                dict.into_any()
            }
        }
    }

    /// The Python object for a date, time, duration, decimal or bytes value.
    fn python_wire_value<'py>(py: Python<'py>, value: &Validated) -> Bound<'py, PyAny> {
        let datetime = py.import("datetime").unwrap();
        match value {
            Validated::Date(day) => datetime
                .getattr("date")
                .unwrap()
                .call1((day.year(), u8::from(day.month()), day.day()))
                .unwrap(),
            Validated::Time(clock) => {
                let zone = clock.offset.map_or_else(
                    || py.None().into_bound(py),
                    |seconds| {
                        let delta = datetime
                            .getattr("timedelta")
                            .unwrap()
                            .call1((0, seconds))
                            .unwrap();
                        datetime
                            .getattr("timezone")
                            .unwrap()
                            .call1((delta,))
                            .unwrap()
                    },
                );
                let keywords = PyDict::new(py);
                keywords.set_item("tzinfo", zone).unwrap();
                datetime
                    .getattr("time")
                    .unwrap()
                    .call(
                        (clock.hour, clock.minute, clock.second, clock.micro),
                        Some(&keywords),
                    )
                    .unwrap()
            }
            Validated::Duration(span) => datetime
                .getattr("timedelta")
                .unwrap()
                .call1((span.days, span.seconds, span.micros))
                .unwrap(),
            Validated::Decimal(text) => py
                .import("decimal")
                .unwrap()
                .getattr("Decimal")
                .unwrap()
                .call1((text,))
                .unwrap(),
            Validated::Bytes(raw) => PyBytes::new(py, raw).into_any(),
            Validated::Net(kind, text) => py
                .import("ipaddress")
                .unwrap()
                .getattr(kind.class_name())
                .unwrap()
                .call1((text,))
                .unwrap(),
            _ => unreachable!("not a wire value"),
        }
    }

    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    enum Verdict {
        Same,
        Deferred,
    }

    fn repr(value: &Bound<'_, PyAny>) -> String {
        value.repr().unwrap().to_string()
    }

    fn compare_data(
        py: Python<'_>,
        compiled: &Compiled,
        python: &Bound<'_, PyAny>,
        native: &Json,
        options: &Options,
    ) -> Verdict {
        let skip: Vec<String> = options.skip_fields.iter().cloned().collect();
        let (expected, expected_problems) = compiled
            .validate(
                py,
                python,
                false,
                options.use_aliases,
                options.allow_naive_datetime,
                skip,
                Some(options.input_bounds),
            )
            .unwrap();
        let schema = compiled.native().expect("the schema lowers");
        let Ok((value, problems)) = schema.validate(native, options) else {
            return Verdict::Deferred;
        };
        let shown = repr(python);
        assert_eq!(problems, expected_problems, "{shown}");
        match (value, expected) {
            (None, None) => {}
            (Some(value), Some(expected)) => {
                let rebuilt = python_value(py, &value);
                assert_eq!(repr(&rebuilt), repr(&expected), "{shown}");
                assert!(rebuilt.eq(&expected).unwrap(), "{shown}");
            }
            (value, expected) => panic!("{shown}: {value:?} vs {expected:?}"),
        }
        Verdict::Same
    }

    fn compare_text(py: Python<'_>, compiled: &Compiled, text: &str, options: &Options) -> Verdict {
        let python = decode_json(py, text.as_bytes()).unwrap();
        let document = decode_json_document(text.as_bytes()).unwrap();
        compare_data(py, compiled, &python, &document.root, options)
    }

    /// Run every body under every option set; return how many were compared
    /// (not deferred).
    fn compare_all(
        py: Python<'_>,
        compiled: &Compiled,
        bodies: &[&str],
        sets: &[Options],
    ) -> usize {
        let mut same = 0;
        for options in sets {
            for body in bodies {
                if compare_text(py, compiled, body, options) == Verdict::Same {
                    same += 1;
                }
            }
        }
        same
    }

    fn option_sets() -> Vec<Options> {
        vec![
            options(true, false, &[]),
            options(false, false, &[]),
            options(true, true, &[]),
            options(true, false, &["count", "user_name", "items", "pet", "n"]),
        ]
    }

    const SCALAR_BODIES: [&str; 46] = [
        r#"{"name": "abc", "count": 3}"#,
        r#"{"name": "", "count": 3}"#,
        r#"{"name": "abcdef", "count": 3}"#,
        r#"{"name": "ABC", "count": 3}"#,
        r#"{"name": "é'é", "count": 0}"#,
        r#"{"name": 5, "count": "3"}"#,
        r#"{"name": null, "count": null}"#,
        r#"{"count": 11}"#,
        r#"{"count": -1, "name": "a"}"#,
        r#"{"count": 1.0, "name": "a"}"#,
        r#"{"count": true, "name": "a"}"#,
        r#"{"count": 99999999999999999999, "name": 99999999999999999999}"#,
        r#"{"name": "a", "count": 1, "ratio": 0}"#,
        r#"{"name": "a", "count": 1, "ratio": 1}"#,
        r#"{"name": "a", "count": 1, "ratio": 1.5}"#,
        r#"{"name": "a", "count": 1, "ratio": -0.0, "flag": 1}"#,
        r#"{"name": "a", "count": 1, "ratio": 1e308, "flag": false}"#,
        r#"{"name": "a", "count": 1, "ratio": true, "flag": "true"}"#,
        r#"{"name": "a", "count": 1, "key": "12345678-1234-5678-1234-567812345678"}"#,
        r#"{"name": "a", "count": 1, "key": "ABCDEF00-1234-5678-1234-567812345678"}"#,
        r#"{"name": "a", "count": 1, "key": "12345678123456781234567812345678"}"#,
        r#"{"name": "a", "count": 1, "key": "{12345678-1234-5678-1234-567812345678}"}"#,
        r#"{"name": "a", "count": 1, "key": 7}"#,
        r#"{"name": "a", "count": 1, "note": "xy", "kind": "b'c"}"#,
        r#"{"name": "a", "count": 1, "note": null, "kind": "é\"x"}"#,
        r#"{"name": "a", "count": 1, "note": "é", "kind": "z"}"#,
        r#"{"name": "a", "count": 1, "kind": 1, "level": 1}"#,
        r#"{"name": "a", "count": 1, "level": 2, "yes": true}"#,
        r#"{"name": "a", "count": 1, "level": 3, "yes": false}"#,
        r#"{"name": "a", "count": 1, "level": 7.0, "yes": 1}"#,
        r#"{"name": "a", "count": 1, "weight": 9007199254740993}"#,
        r#"{"name": "a", "count": 1, "weight": 9007199254740994}"#,
        r#"{"name": "a", "count": 1, "weight": 9007199254740992.0}"#,
        r#"{"name": "a", "count": 1, "weight": 1e300, "zero": 0}"#,
        r#"{"name": "a", "count": 1, "weight": 3, "zero": -0.0}"#,
        r#"{"name": "a", "count": 1, "zero": 0.5, "small": 0}"#,
        r#"{"name": "a", "count": 1, "zero": 1, "small": 1}"#,
        r#"{"name": "a", "count": 1, "small": 2, "nan": 1}"#,
        r#"{"name": "a", "count": 1, "nan": 1e308, "edge": 9223372036854775807}"#,
        r#"{"name": "a", "count": 1, "edge": -9223372036854775808, "wide": 9223372036854775807}"#,
        r#"{"name": "a", "count": 1, "wide": -9223372036854775808, "digit": 11}"#,
        r#"{"name": "a", "count": 1, "digit": 12, "inf": 1e308}"#,
        r#"{"name": "a", "count": 1, "inf": -1e308, "extra": 1, "ÄÖÜ": 2}"#,
        r#"{"name": "a", "count": 1, "name": "abcdef", "count": 2}"#,
        r#"[]"#,
        r#""text""#,
    ];

    const DATETIMES: [&str; 22] = [
        "2026-11-13T10:00:00Z",
        "2026-11-13T10:00:00+01:00",
        "2026-11-13T10:00:00-05:30",
        "2026-11-13T10:00:00-00:00",
        "2026-11-13T10:00:00+23:59",
        "2026-11-13T10:00:00.1Z",
        "2026-11-13T10:00:00.123456+02:00",
        "2026-11-13T10:00:00.1234567Z",
        "2026-11-13T10:00:00",
        "2026-11-13T23:59:59.999999",
        "0001-01-01T00:00:00+01:00",
        "9999-12-31T23:59:59-01:00",
        "2024-02-29T00:00:00Z",
        "2026-02-29T00:00:00Z",
        "0000-01-01T00:00:00Z",
        "2026-11-13T24:00:00Z",
        "2026-11-13T10:00:60Z",
        "2026-11-13T10:00:00+24:00",
        "2026-11-13t10:00:00Z",
        "2026-11-13T10:00:00z",
        "2026-11-13",
        "2026-11-13 10:00:00Z",
    ];

    #[test]
    fn scalar_constraints_report_the_same_problems_and_values() {
        Python::initialize();
        Python::attach(|py| {
            let compiled = compile(py, SCALARS);
            let same = compare_all(py, &compiled, &SCALAR_BODIES, &option_sets());
            // Only the body with an integer outside i64 in `count` defers,
            // except where `count` is skipped.
            assert_eq!(same, SCALAR_BODIES.len() * option_sets().len() - 3);
        });
    }

    #[test]
    fn datetimes_match_or_defer_and_never_disagree() {
        Python::initialize();
        Python::attach(|py| {
            let compiled = compile(py, SCALARS);
            let mut same = 0;
            for text in DATETIMES {
                for allow_naive in [false, true] {
                    let body = format!(r#"{{"name": "a", "count": 1, "at": "{text}"}}"#);
                    let options = options(true, allow_naive, &[]);
                    if compare_text(py, &compiled, &body, &options) == Verdict::Same {
                        same += 1;
                    }
                }
            }
            // Twelve certain shapes in both modes, two rejected by shape.
            assert_eq!(same, 2 * 14);
            let shaped = r#"{"name": "a", "count": 1, "at": "2026-02-29T00:00:00Z"}"#;
            let document = decode_json_document(shaped.as_bytes()).unwrap();
            let native = compiled.native().unwrap();
            assert_eq!(
                native.validate(&document.root, &options(true, false, &[])),
                Err(UNCERTAIN_DATETIME)
            );
        });
    }

    #[test]
    fn aliases_and_unknown_fields_report_the_same_problems() {
        Python::initialize();
        Python::attach(|py| {
            let compiled = compile(py, ALIASES);
            let long_key = "é".repeat(80);
            let long = format!(r#"{{"userName": "a", "{long_key}": 1}}"#);
            let bodies = [
                r#"{"userName": "a"}"#,
                r#"{"user_name": "a"}"#,
                r#"{"userName": "a", "user_name": "b"}"#,
                r#"{"user_name": "b", "userName": 5, "age": null}"#,
                r#"{"userName": "a", "age": "1", "other": 1, "x\"y": 2}"#,
                r#"{}"#,
                long.as_str(),
            ];
            let same = compare_all(py, &compiled, &bodies, &option_sets());
            assert_eq!(same, bodies.len() * option_sets().len());
        });
    }

    #[test]
    fn nested_objects_and_lists_report_the_same_paths() {
        Python::initialize();
        Python::attach(|py| {
            let compiled = compile(py, NESTED);
            let bodies = [
                r#"{"items": [{"id": 1}]}"#,
                r#"{"items": []}"#,
                r#"{"items": [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}]}"#,
                r#"{"items": [{"id": "1", "label": "x"}, 5, {"label": "ok", "more": 1}]}"#,
                r#"{"items": {"id": 1}, "child": [], "tags": "ab"}"#,
                r#"{"items": [{"id": 1}], "child": null, "tags": null}"#,
                r#"{"items": [{"id": 1}], "child": {"id": 2, "label": "é"}, "tags": ["ab", "abc", 1]}"#,
                r#"{"items": [{"id": 1}], "grid": [[0, 1], [2, -1], [1, 2, 3], 4], "x": 1}"#,
                r#"{"items": [{"id": 1}], "grid": []}"#,
            ];
            let same = compare_all(py, &compiled, &bodies, &option_sets());
            assert_eq!(same, bodies.len() * option_sets().len());
        });
    }

    #[test]
    fn tagged_unions_report_the_same_problems() {
        Python::initialize();
        Python::attach(|py| {
            let compiled = compile(py, UNION);
            let bodies = [
                r#"{"pet": {"type": "cat", "lives": 3}}"#,
                r#"{"pet": {"type": "dog", "good": true, "extra": 1}}"#,
                r#"{"pet": {"type": "cow"}}"#,
                r#"{"pet": {"type": 5}}"#,
                r#"{"pet": {"lives": 3}}"#,
                r#"{"pet": {"kind": "cat", "type": "cat", "lives": 3}}"#,
                r#"{"pet": {"kind": "cat", "lives": 3}}"#,
                r#"{"pet": {"type": "cat", "lives": 10, "x": 1}}"#,
                r#"{"pet": [], "pets": [null, {"kind": "dog"}, {"type": "dog"}, 3]}"#,
                r#"{"pet": {"type": "dog"}, "pets": [{"kind": "cat", "type": "cat", "lives": 1}]}"#,
            ];
            let same = compare_all(py, &compiled, &bodies, &option_sets());
            assert_eq!(same, bodies.len() * option_sets().len());
        });
    }

    #[test]
    fn mappings_report_the_same_problems_and_values() {
        Python::initialize();
        Python::attach(|py| {
            let compiled = compile(py, MAPPING);
            let long_key = "k".repeat(70);
            let long = format!(
                r#"{{"labels": {{"{long_key}": "x"}}, "points": {{"{long_key}": {{"x": 1}}}}}}"#
            );
            let bodies = [
                r#"{"labels": {"b": "x", "a": "yy"}}"#,
                r#"{"labels": {"b": "x", "a": "yy", "b": "z"}}"#,
                r#"{"labels": {}}"#,
                r#"{"labels": {"a": "x", "b": "x", "c": "x", "d": "x"}}"#,
                r#"{"labels": {"a": 1, "b": "toolong", "c": null}}"#,
                r#"{"labels": {"abcde": "x", "__proto__": "y"}}"#,
                r#"{"labels": [], "points": "x", "grid": 3}"#,
                r#"{"labels": {"a": "x"}, "points": {"ab": {"x": 1}, "a": {"x": 2}, "AB": {"x": 3}}}"#,
                r#"{"labels": {"a": "x"}, "points": {"ab": {"y": -1, "z": 1}, "cd": 5}}"#,
                r#"{"labels": {"a": "x"}, "grid": null}"#,
                r#"{"labels": {"a": "x"}, "grid": {}}"#,
                r#"{"labels": {"a": "x"}, "grid": {"r": [1, "2"], "n": null, "e": []}}"#,
                r#"{"labels": {"é": "é"}, "grid": {"r": [99999999999999999999]}}"#,
                long.as_str(),
            ];
            let same = compare_all(py, &compiled, &bodies, &option_sets());
            // Only the integer outside i64 inside `grid` defers.
            assert_eq!(
                same,
                bodies.len() * option_sets().len() - option_sets().len()
            );
        });
    }

    #[test]
    fn the_error_cap_and_the_depth_limit_are_shared() {
        Python::initialize();
        Python::attach(|py| {
            let compiled = compile(py, ALIASES);
            let mut object = JsonObject::default();
            for index in 0..60 {
                object.insert(format!("unknown{index}"), Json::Int(index));
            }
            let many = Json::Object(object);
            let python = json_to_python(py, &many).unwrap();
            let options = options(true, false, &[]);
            assert_eq!(
                compare_data(py, &compiled, &python, &many, &options),
                Verdict::Same
            );
            let (_, problems) = compiled
                .native()
                .unwrap()
                .validate(&many, &options)
                .unwrap();
            assert_eq!(problems.len(), MAX_ERRORS + 1);
            assert_eq!(problems[MAX_ERRORS].1, "too_many_errors");

            let chain = compile(py, CHAIN);
            for levels in [127, 128, 129, 140] {
                let mut value = Json::Object(JsonObject::default());
                for _ in 0..levels {
                    let mut outer = JsonObject::default();
                    outer.insert("next".into(), value);
                    value = Json::Object(outer);
                }
                let python = json_to_python(py, &value).unwrap();
                assert_eq!(
                    compare_data(py, &chain, &python, &value, &options),
                    Verdict::Same
                );
                let (_, problems) = chain.native().unwrap().validate(&value, &options).unwrap();
                let deep = problems.first().is_some_and(|problem| problem.1 == "depth");
                assert_eq!(deep, levels > MAX_DEPTH, "{levels}");
            }
        });
    }

    #[test]
    fn integers_outside_i64_defer_only_where_python_reads_them_as_numbers() {
        Python::initialize();
        Python::attach(|py| {
            let compiled = compile(py, SCALARS);
            let options = options(true, false, &[]);
            let text_field = r#"{"name": 99999999999999999999, "count": 1}"#;
            assert_eq!(
                compare_text(py, &compiled, text_field, &options),
                Verdict::Same
            );
            for body in [
                r#"{"name": "a", "count": 99999999999999999999}"#,
                r#"{"name": "a", "count": 1, "ratio": 99999999999999999999}"#,
            ] {
                assert_eq!(
                    compare_text(py, &compiled, body, &options),
                    Verdict::Deferred
                );
            }
        });
    }

    #[test]
    fn constraints_without_an_exact_rust_equivalent_are_not_lowered() {
        Python::initialize();
        Python::attach(|py| {
            for constraint in [
                r#"node("int", ge=decimal.Decimal(1))"#,
                r#"node("int", ge=True)"#,
                r#"node("int", le=2**70)"#,
                r#"node("str", pattern=Text("a+"))"#,
                r#"node("str", one_of=("a", Text("b")))"#,
                r#"node("str", one_of=("\ud800",))"#,
                r#"node("int", one_of=(1, 2**70))"#,
                r#"node("int", one_of=(None,))"#,
                r#"node("uuid", one_of=("12345678-1234-5678-1234-567812345678",))"#,
                r#"node("list", item=node("int", ge=decimal.Decimal(1)))"#,
                r#"node("dict", item=node("int", ge=decimal.Decimal(1)), key=node("str"))"#,
                r#"node("dict", item=node("int"), key=node("str", pattern=Text("a+")))"#,
            ] {
                let table = format!(r#"[(Made, [("f", "f", {constraint}, True)], False)]"#);
                assert!(compile(py, &table).native().is_none(), "{constraint}");
            }
            for constraint in [
                r#"node("int", ge=-1, le=2.5, gt=-2**63, lt=2**63 - 1)"#,
                r#"node("str", pattern="a+", one_of=("a", "aa"))"#,
                r#"node("bool", one_of=(True, 0, 1.0))"#,
                r#"node("datetime", ge=decimal.Decimal(1))"#,
            ] {
                let table = format!(r#"[(Made, [("f", "f", {constraint}, True)], False)]"#);
                let lowered = compile(py, &table);
                assert!(lowered.native().is_some(), "{constraint}");
            }
        });
    }

    #[test]
    fn python_number_comparison_is_exact_across_int_and_float() {
        let big = Number::Int(9_007_199_254_740_993);
        assert_eq!(
            compare_numbers(big, Number::Float(9_007_199_254_740_992.0)),
            Some(Ordering::Greater)
        );
        assert_eq!(
            compare_numbers(Number::Float(9_007_199_254_740_992.0), big),
            Some(Ordering::Less)
        );
        assert_eq!(
            compare_numbers(Number::Int(i64::MAX), Number::Float(TWO_POW_63)),
            Some(Ordering::Less)
        );
        assert_eq!(
            compare_numbers(Number::Int(i64::MIN), Number::Float(-TWO_POW_63)),
            Some(Ordering::Equal)
        );
        assert_eq!(
            compare_numbers(Number::Int(i64::MIN), Number::Float(f64::NEG_INFINITY)),
            Some(Ordering::Greater)
        );
        assert_eq!(
            compare_numbers(Number::Int(-1), Number::Float(-1.5)),
            Some(Ordering::Greater)
        );
        assert_eq!(
            compare_numbers(Number::Int(-2), Number::Float(-1.5)),
            Some(Ordering::Less)
        );
        assert_eq!(
            compare_numbers(Number::Int(0), Number::Float(-0.0)),
            Some(Ordering::Equal)
        );
        assert_eq!(
            compare_numbers(Number::Int(0), Number::Float(f64::NAN)),
            None
        );
        assert!(holds(CompareOp::Ne, None));
        assert!(!holds(CompareOp::Ge, None));
        assert!(holds(CompareOp::Ne, Some(Ordering::Less)));
    }

    /// A small deterministic generator (xorshift64).
    struct Draw(u64);

    impl Draw {
        fn next(&mut self, below: usize) -> usize {
            self.0 ^= self.0 << 13;
            self.0 ^= self.0 >> 7;
            self.0 ^= self.0 << 17;
            usize::try_from(self.0 % u64::try_from(below).unwrap()).unwrap()
        }
    }

    const VALUES: [&str; 44] = [
        "null",
        "true",
        "false",
        "0",
        "-1",
        "1",
        "2",
        "3",
        "7",
        "11",
        "1.5",
        "0.5",
        "-0.0",
        "1e308",
        "9007199254740993",
        "9223372036854775807",
        "\"\"",
        "\"a\"",
        "\"ab\"",
        "\"abcdef\"",
        "\"é'\"",
        "\"b'c\"",
        "\"é\\\"x\"",
        "\"cat\"",
        "\"dog\"",
        "\"12345678-1234-5678-1234-567812345678\"",
        "\"2026-11-13T10:00:00Z\"",
        "\"2026-11-13T10:00:00\"",
        "\"2026-11-13T10:00:00.5+01:00\"",
        "[]",
        "[1]",
        "{}",
        "[\"ab\", \"abc\"]",
        "{\"id\": 1}",
        "{\"id\": \"x\", \"label\": \"a\"}",
        "[{\"id\": 1}, {\"id\": 2, \"label\": \"no\"}]",
        "{\"type\": \"cat\", \"lives\": 3}",
        "{\"type\": \"dog\", \"good\": 1}",
        "{\"type\": 5}",
        "{\"kind\": \"cat\", \"type\": \"dog\"}",
        "[[1, 2], [3, -1]]",
        "[null, {\"kind\": \"dog\"}]",
        "{\"next\": {\"next\": null, \"n\": 1}}",
        "\"\\u00e9\\ud83d\\ude00\"",
    ];

    const KEYS: [&str; 34] = [
        "name",
        "count",
        "ratio",
        "flag",
        "key",
        "at",
        "note",
        "kind",
        "level",
        "yes",
        "weight",
        "zero",
        "small",
        "nan",
        "edge",
        "wide",
        "digit",
        "inf",
        "userName",
        "user_name",
        "age",
        "items",
        "child",
        "tags",
        "grid",
        "pet",
        "pets",
        "next",
        "n",
        "type",
        "extra",
        "é",
        "labels",
        "points",
    ];

    /// Values a `dict` field of [`MAPPING`] accepts or nearly accepts.
    const MAP_VALUES: [&str; 12] = [
        "{}",
        "{\"a\": \"x\"}",
        "{\"a\": \"x\", \"bb\": \"yy\"}",
        "{\"__proto__\": \"z\"}",
        "{\"a\": 1}",
        "{\"abcde\": \"x\"}",
        "{\"ab\": {\"x\": 1}}",
        "{\"ab\": {\"x\": 1, \"y\": 2}, \"cd\": {\"x\": -3}}",
        "{\"A\": {\"x\": 1}}",
        "{\"r\": [1, 2], \"n\": null}",
        "{\"r\": [\"1\"]}",
        "{\"a\": \"x\", \"a\": \"yy\"}",
    ];

    const MAP_KEYS: [&str; 4] = ["labels", "labels", "points", "grid"];

    fn random_body(draw: &mut Draw) -> String {
        random_body_of(draw, &KEYS, &VALUES)
    }

    fn random_body_of(draw: &mut Draw, keys: &[&str], values: &[&str]) -> String {
        let members: Vec<String> = (0..draw.next(7))
            .map(|_| {
                let key = keys[draw.next(keys.len())];
                let value = values[draw.next(values.len())];
                format!("\"{key}\": {value}")
            })
            .collect();
        format!("{{{}}}", members.join(", "))
    }

    #[test]
    fn random_documents_never_disagree() {
        Python::initialize();
        Python::attach(|py| {
            let mut draw = Draw(0x9E37_79B9_7F4A_7C15);
            for definitions in [SCALARS, ALIASES, NESTED, UNION, MAPPING, CHAIN] {
                let compiled = compile(py, definitions);
                let mut same = 0;
                let sets = option_sets();
                for round in 0..600 {
                    let body = random_body(&mut draw);
                    if compare_text(py, &compiled, &body, &sets[round % sets.len()])
                        == Verdict::Same
                    {
                        same += 1;
                    }
                }
                assert!(same > 450, "{same} of 600 compared");
            }
        });
    }

    /// Whether a native result holds a non-empty accepted `labels` map.
    fn accepted_map(value: Option<&Validated>) -> bool {
        let Some(Validated::Object(fields)) = value else {
            return false;
        };
        fields.iter().any(|(name, item)| {
            name == "labels" && matches!(item, Validated::Object(entries) if !entries.is_empty())
        })
    }

    #[test]
    fn random_mapping_documents_never_disagree_and_are_often_accepted() {
        Python::initialize();
        Python::attach(|py| {
            let mut draw = Draw(0x2545_F491_4F6C_DD1D);
            let compiled = compile(py, MAPPING);
            let native = compiled.native().unwrap();
            // Junk values are drawn by `random_documents_never_disagree`; these
            // stay close to valid so accepted maps are reached too.
            let values: Vec<&str> = MAP_VALUES.iter().chain(&VALUES[..4]).copied().collect();
            let sets = option_sets();
            let (mut same, mut accepted) = (0, 0);
            for round in 0..600 {
                let body = random_body_of(&mut draw, &MAP_KEYS, &values);
                let options = &sets[round % sets.len()];
                if compare_text(py, &compiled, &body, options) == Verdict::Same {
                    same += 1;
                }
                let document = decode_json_document(body.as_bytes()).unwrap();
                if let Ok((value, _)) = native.validate(&document.root, options) {
                    accepted += usize::from(accepted_map(value.as_ref()));
                }
            }
            assert!(same > 450, "{same} of 600 compared");
            assert!(accepted > 10, "{accepted} accepted non-empty maps");
        });
    }

    const BOUNDED: &str = r#"[(Made, [
        ("caps", "caps", node("dict", item=node("int"), key=node("str", input_max_length=3),
                              input_max_length=2), True),
        ("open", "open", node("dict", nullable=True, item=node("int"), key=node("str", max_length=5,
                              input_max_length=3), max_length=4, input_max_length=1), False),
    ], True)]"#;

    #[test]
    fn wire_input_bounds_apply_only_when_asked_on_both_paths() {
        Python::initialize();
        Python::attach(|py| {
            let compiled = compile(py, BOUNDED);
            let bodies = [
                r#"{"caps": {"a": 1, "b": 2}}"#,
                r#"{"caps": {"a": 1, "b": 2, "c": 3}}"#,
                r#"{"caps": {"abcd": 1}}"#,
                r#"{"caps": {}, "open": {"abcd": 1, "b": 2, "c": 3, "d": 4}}"#,
                r#"{"caps": {}, "open": {"abcdef": 1, "b": 2, "c": 3, "d": 4, "e": 5}}"#,
            ];
            let mut wire = options(true, false, &[]);
            wire.input_bounds = true;
            let sets = [options(true, false, &[]), wire];
            let same = compare_all(py, &compiled, &bodies, &sets);
            assert_eq!(same, bodies.len() * sets.len());
            let native = compiled.native().unwrap();
            let problems = |text: &str, options: &Options| {
                let document = decode_json_document(text.as_bytes()).unwrap();
                native.validate(&document.root, options).unwrap().1
            };
            assert!(problems(bodies[1], &sets[0]).is_empty());
            assert_eq!(problems(bodies[1], &sets[1])[0].1, "max_length");
            assert_eq!(problems(bodies[2], &sets[1])[0].1, "key_max_length");
            // An explicit max_length wins over the wire-input bound.
            assert!(problems(bodies[3], &sets[1]).is_empty());
            let codes: Vec<&str> = problems(bodies[4], &sets[0]).iter().map(|p| p.1).collect();
            assert_eq!(codes, ["max_length"]);
        });
    }
}

const DUMP_DEPTH: usize = 1000;

struct SerializationOptions<'a> {
    json_mode: bool,
    by_alias: bool,
    exclude_unset: bool,
    controlled: bool,
    exclude: &'a [String],
    include: Option<&'a [String]>,
}

/// A value as `Schema.dump()` (JSON-compatible) or `Schema.to_dict()` (Python
/// values) shows it: nested schemas become dicts, lists and tuples lists, dicts
/// are copied; in JSON mode exact `UUID`/`datetime` values become text and any
/// object with a `dump()` method is replaced by what it returns.
fn convert<'py>(
    value: &Bound<'py, PyAny>,
    json_mode: bool,
    by_alias: bool,
    exclude_unset: bool,
    controlled: bool,
    depth: usize,
) -> PyResult<Bound<'py, PyAny>> {
    let py = value.py();
    if depth > DUMP_DEPTH {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "value is nested too deeply",
        ));
    }
    if crate::pytypes::is_plain_scalar(value) {
        return Ok(value.clone());
    }
    if let Ok(list) = value.cast::<PyList>() {
        return convert_list(
            py,
            list.iter(),
            json_mode,
            by_alias,
            exclude_unset,
            controlled,
            depth,
        );
    }
    if let Some(grouped) =
        convert_grouped(value, json_mode, by_alias, exclude_unset, controlled, depth)?
    {
        return Ok(grouped);
    }
    if let Ok(dict) = value.cast::<PyDict>() {
        return convert_dict(
            py,
            dict,
            json_mode,
            by_alias,
            exclude_unset,
            controlled,
            depth,
        );
    }
    if instance_fields(value, false, by_alias)?.is_some() {
        return convert_schema(value, json_mode, by_alias, exclude_unset, controlled, depth);
    }
    if json_mode {
        return convert_json_object(value);
    }
    Ok(value.clone())
}

/// A tuple, set or frozenset: JSON mode writes a list (a set sorted when its
/// members compare); Python mode keeps the container.
fn convert_grouped<'py>(
    value: &Bound<'py, PyAny>,
    json_mode: bool,
    by_alias: bool,
    exclude_unset: bool,
    controlled: bool,
    depth: usize,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = value.py();
    let is_set = value.is_instance_of::<pyo3::types::PySet>()
        || value.is_instance_of::<pyo3::types::PyFrozenSet>();
    if !is_set && !value.is_instance_of::<pyo3::types::PyTuple>() {
        return Ok(None);
    }
    if is_set && !json_mode {
        return Ok(Some(value.clone()));
    }
    let members = if is_set {
        crate::pytypes::sorted_members(value)?
    } else {
        value.try_iter()?.collect::<PyResult<Vec<_>>>()?
    };
    let items = convert_list(
        py,
        members.into_iter(),
        json_mode,
        by_alias,
        exclude_unset,
        controlled,
        depth,
    )?;
    if json_mode {
        return Ok(Some(items));
    }
    Ok(Some(py.get_type::<pyo3::types::PyTuple>().call1((items,))?))
}

fn convert_list<'py>(
    py: Python<'py>,
    items: impl Iterator<Item = Bound<'py, PyAny>>,
    json_mode: bool,
    by_alias: bool,
    exclude_unset: bool,
    controlled: bool,
    depth: usize,
) -> PyResult<Bound<'py, PyAny>> {
    let out = PyList::empty(py);
    for item in items {
        out.append(convert(
            &item,
            json_mode,
            by_alias,
            exclude_unset,
            controlled,
            depth + 1,
        )?)?;
    }
    Ok(out.into_any())
}

fn convert_dict<'py>(
    py: Python<'py>,
    items: &Bound<'py, PyDict>,
    json_mode: bool,
    by_alias: bool,
    exclude_unset: bool,
    controlled: bool,
    depth: usize,
) -> PyResult<Bound<'py, PyAny>> {
    let out = PyDict::new(py);
    for (key, item) in items.iter() {
        out.set_item(
            key,
            convert(
                &item,
                json_mode,
                by_alias,
                exclude_unset,
                controlled,
                depth + 1,
            )?,
        )?;
    }
    Ok(out.into_any())
}

fn convert_schema<'py>(
    value: &Bound<'py, PyAny>,
    json_mode: bool,
    by_alias: bool,
    exclude_unset: bool,
    controlled: bool,
    depth: usize,
) -> PyResult<Bound<'py, PyAny>> {
    if json_mode && instance_fields(value, true, by_alias)?.is_none() {
        if controlled {
            let class_name = value.get_type().name()?.to_string();
            return Err(pyo3::exceptions::PyTypeError::new_err(format!(
                "controlled serialization cannot inspect {class_name} because it overrides dump(); use its dump() or a projection Schema"
            )));
        }
        return value.call_method0("dump"); // the class decides what it exposes
    }
    Ok(dump_instance_at(
        value,
        SerializationOptions {
            json_mode,
            by_alias,
            exclude_unset,
            controlled,
            exclude: &[],
            include: None,
        },
        depth + 1,
    )?
    .into_any())
}

fn convert_json_object<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    if let Some(text) = crate::pytypes::exact_uuid_text(value)? {
        return Ok(pyo3::types::PyString::new(value.py(), &text).into_any());
    }
    if value
        .get_type()
        .is(crate::pytypes::datetime_class(value.py())?)
    {
        return value.call_method0("isoformat");
    }
    if let Some(text) = wire_text(value)? {
        return Ok(pyo3::types::PyString::new(value.py(), &text).into_any());
    }
    if let Some(member) = crate::pytypes::enum_value(value)? {
        return Ok(member);
    }
    if value.hasattr("dump")? {
        return value.call_method0("dump");
    }
    Ok(value.clone())
}

fn dump_instance_at<'py>(
    value: &Bound<'py, PyAny>,
    options: SerializationOptions<'_>,
    depth: usize,
) -> PyResult<Bound<'py, PyDict>> {
    let py = value.py();
    let (names, unset) = dump_schema_fields(value, &options)?;
    let supplied = supplied_fields(value, options.exclude_unset)?;
    let out = PyDict::new(py);
    for (name, wire_name) in names {
        if skip_serialized_field(py, &name, &options, supplied.as_ref())? {
            continue;
        }
        let item = value.getattr(name.as_str())?;
        if item.is(&unset) {
            continue;
        }
        out.set_item(
            &wire_name,
            convert(
                &item,
                options.json_mode,
                options.by_alias,
                options.exclude_unset,
                options.controlled,
                depth,
            )?,
        )?;
    }
    Ok(out)
}

fn dump_schema_fields(
    value: &Bound<'_, PyAny>,
    options: &SerializationOptions<'_>,
) -> PyResult<(SchemaWireNames, Py<PyAny>)> {
    if let Some(fields) = instance_fields(value, options.controlled, options.by_alias)? {
        return Ok(fields);
    }
    if options.controlled {
        let class_name = value.get_type().name()?.to_string();
        return Err(pyo3::exceptions::PyTypeError::new_err(format!(
            "controlled serialization cannot inspect {class_name} because it overrides dump(); use its dump() or a projection Schema"
        )));
    }
    Err(pyo3::exceptions::PyTypeError::new_err(
        "not a schema instance",
    ))
}

fn supplied_fields<'py>(
    value: &Bound<'py, PyAny>,
    exclude_unset: bool,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    if !exclude_unset {
        return Ok(None);
    }
    let marker = value.getattr("__hinoki_supplied_fields__").map_err(|_| {
        pyo3::exceptions::PyTypeError::new_err(format!(
            "{} has no reliable supplied-field tracking; exclude_unset requires the generated Schema constructor",
            value
                .get_type()
                .name()
                .map(|name| name.to_string())
                .unwrap_or_else(|_| "Schema".to_owned())
        ))
    })?;
    Ok(Some(marker))
}

fn skip_serialized_field<'py>(
    py: Python<'py>,
    name: &str,
    options: &SerializationOptions<'_>,
    supplied: Option<&Bound<'py, PyAny>>,
) -> PyResult<bool> {
    if options.exclude.iter().any(|field| field == name) {
        return Ok(true);
    }
    if options
        .include
        .is_some_and(|selected| !selected.iter().any(|field| field == name))
    {
        return Ok(true);
    }
    if let Some(fields) = supplied {
        if !fields.is(PyBool::new(py, true))
            && !fields.call_method1("__contains__", (name,))?.is_truthy()?
        {
            return Ok(true);
        }
    }
    Ok(false)
}

/// The fields of a schema instance as a dict (UNSET omitted): JSON-compatible
/// when `json_mode`, Python values otherwise. `exclude` names Python fields to
/// skip; `by_alias` selects their wire names in the resulting mapping.
#[pyfunction]
#[pyo3(signature = (value, json_mode, exclude=Vec::new(), by_alias=true))]
pub fn dump_instance<'py>(
    value: &Bound<'py, PyAny>,
    json_mode: bool,
    exclude: Vec<String>,
    by_alias: bool,
) -> PyResult<Bound<'py, PyDict>> {
    dump_instance_at(
        value,
        SerializationOptions {
            json_mode,
            by_alias,
            exclude_unset: false,
            controlled: false,
            exclude: &exclude,
            include: None,
        },
        0,
    )
}

/// JSON-compatible, explicitly selected Schema projection.
#[pyfunction]
#[pyo3(signature = (value, include=None, exclude=Vec::new(), exclude_unset=false, by_alias=true))]
pub fn serialize_instance<'py>(
    value: &Bound<'py, PyAny>,
    include: Option<Vec<String>>,
    exclude: Vec<String>,
    exclude_unset: bool,
    by_alias: bool,
) -> PyResult<Bound<'py, PyDict>> {
    dump_instance_at(
        value,
        SerializationOptions {
            json_mode: true,
            by_alias,
            exclude_unset,
            controlled: exclude_unset || !by_alias,
            exclude: &exclude,
            include: include.as_deref(),
        },
        0,
    )
}

/// How one model attribute converts for a schema field: a nested model becomes
/// `schema.from_model(value)`, a list or tuple maps its items through `item`
/// into a new list, a dict (for a `dict` field) its values into a new dict,
/// anything else passes through unchanged.
struct Conversion {
    schema: Option<Py<PyAny>>,
    item: Option<Box<Conversion>>,
    mapping: bool,
}

fn parse_conversion(raw: &Bound<'_, PyAny>) -> PyResult<Conversion> {
    let (schema, item, mapping): (Option<Py<PyAny>>, Option<Bound<'_, PyAny>>, bool) =
        raw.extract()?;
    Ok(Conversion {
        schema,
        item: match item {
            Some(inner) => Some(Box::new(parse_conversion(&inner)?)),
            None => None,
        },
        mapping,
    })
}

struct PlanField {
    key: Py<pyo3::types::PyString>,
    name: String,
    required: bool,
    conversion: Conversion,
}

/// The fields of a schema as `from_model` reads them, prepared once per class.
#[pyclass(name = "ModelPlan", module = "hinoki.core")]
pub struct ModelPlan {
    class: Py<PyAny>,
    fields: Vec<PlanField>,
    direct: bool,
}

#[pymethods]
impl ModelPlan {
    /// `fields` is `[(name, required, (schema, item)), ...]`; `direct` says the
    /// class uses the generated constructor (no `__init__`/`__post_init__` of its
    /// own), so instances may be built without calling it.
    #[new]
    #[pyo3(signature = (class, fields, direct=true))]
    fn new(
        class: Py<PyAny>,
        fields: Vec<(String, bool, Bound<'_, PyAny>)>,
        direct: bool,
    ) -> PyResult<Self> {
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
            direct,
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
        if self.direct && values.len() == self.fields.len() {
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
            instance
                .getattr("__dict__")?
                .cast::<PyDict>()?
                .set_item("__hinoki_supplied_fields__", true)?;
            return Ok(instance);
        }
        class.call_method1("__hinoki_construct_from_model__", (values,))
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
        if conversion.mapping {
            return convert_mapping(value, inner);
        }
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

/// A dict attribute copied with its values converted; anything else is left for
/// validation to report.
fn convert_mapping<'py>(
    value: &Bound<'py, PyAny>,
    inner: &Conversion,
) -> PyResult<Bound<'py, PyAny>> {
    let Ok(dict) = value.cast::<PyDict>() else {
        return Ok(value.clone());
    };
    let out = PyDict::new(value.py());
    for (key, element) in dict.iter() {
        out.set_item(key, convert_attribute(&element, inner)?)?;
    }
    Ok(out.into_any())
}
