use std::borrow::Cow;
use std::collections::HashSet;
use std::sync::Arc;

use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyDict, PyString};
use regex::Regex;
use time::{Date, OffsetDateTime, PrimitiveDateTime};
use uuid::Uuid;

use super::internet::NetKind;
use super::narrow::Guide;
use super::scalars::{ClockTime, DecimalNumber, Duration};
use super::typeddecode;

pub type SchemaWireNames = Vec<(String, String)>;
/// Wire names, the `UNSET` sentinel and how many trailing names are computed.
pub type SchemaInstanceFields = Option<(SchemaWireNames, Py<PyAny>, usize)>;
/// What each declared field's annotation says about nested schemas, by position.
pub(crate) type FieldGuides = Arc<[Option<Guide>]>;

/// One pre-resolved field entry in a schema's cached JSON encoding plan.
pub(crate) struct JsonFieldEntry {
    pub(crate) attr: Py<PyString>,
    pub(crate) wire_name: String,
    pub(crate) quoted_key: Box<[u8]>,
}

/// Cached, immutable field plan used by the JSON encoder for a `Schema` class.
pub(crate) struct JsonFieldPlan {
    pub(crate) entries: Box<[JsonFieldEntry]>,
    pub(crate) unset: Py<PyAny>,
    pub(crate) streamable: bool,
    /// The declared fields' guides; computed entries follow with none.
    pub(crate) guides: FieldGuides,
    /// Whether any field declares a nested schema (otherwise nothing to narrow).
    pub(crate) guided: bool,
    pub(super) computed_source: Option<Py<PyAny>>,
}

pub(super) const MAX_ERRORS: usize = 50;
pub(super) const MAX_KEY_ECHO: usize = 64;
pub(super) const MAX_DEPTH: usize = 128;
/// The digits a `Decimal` field accepts unless it declares `max_digits`.
pub(super) const MAX_DECIMAL_DIGITS: i64 = 64;

pub(crate) type Problem = (String, &'static str, String);
/// The entries of a `dict` input: each key as text (`None` when it is not
/// valid Unicode) and its value.
pub(super) type Entries<D> = Vec<(Option<String>, D)>;
pub(super) type FieldTable<'py> = Vec<(String, String, Bound<'py, PyAny>, bool)>;

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
    pub(super) object: Py<PyAny>,
    pub(super) direct: Option<Direct>,
}

/// Instances of a class with the generated constructor, no `__post_init__`, no
/// custom `__new__` and no normalizers: a record holding every field is already
/// final, so the instance is allocated and filled here.
pub(crate) struct Direct {
    /// The number of fields: a record shorter than that leaves defaults to fill.
    pub(super) fields: usize,
    /// The tracking value for `__hinoki_supplied_fields__` (every field name),
    /// or `None` when the class does not track it.
    pub(super) supplied: Option<Py<PyAny>>,
    /// The position of the class in the table.
    pub(super) index: usize,
    /// The dataclass defaults and default factories of its fields, by name.
    pub(super) defaults: Option<(Py<PyDict>, Py<PyDict>)>,
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
    pub(super) value: LimitValue,
    pub(super) shown: String,
}

impl NativeLimit {
    /// The bound as a number, `None` for a `Decimal` bound.
    pub(super) fn number(&self) -> Option<Number> {
        match self.value {
            LimitValue::Number(number) => Some(number),
            LimitValue::Decimal(_) => None,
        }
    }
}

/// A bound of a number field, or of a `Decimal` field (compared exactly).
pub(super) enum LimitValue {
    Number(Number),
    Decimal(DecimalNumber),
}

#[derive(Debug, Clone, PartialEq)]
pub(super) enum Choice {
    /// `bool` choices are stored as `0`/`1`: Python compares them as ints.
    Number(Number),
    Str(String),
}

/// Lowered `one_of` choices and the message text Python would show.
pub(crate) struct NativeChoices {
    pub(super) values: Vec<Choice>,
    pub(super) shown: String,
}

pub(super) enum Kind<R: Rules> {
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
    /// A `pathlib.Path`, read from text.
    Path,
    /// A `Secret`: text with the string rules, wrapped to stay masked.
    Secret,
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
pub(super) enum Collection {
    Set,
    Frozenset,
    Tuple,
}

pub(super) struct Node<R: Rules> {
    pub(super) kind: Kind<R>,
    /// The container that replaces the checked array.
    pub(super) collection: Option<Collection>,
    pub(super) nullable: bool,
    pub(super) min_length: Option<i64>,
    pub(super) max_length: Option<i64>,
    /// The framework bound used on wire input when `max_length` is unset (the
    /// entries of a dict, the characters of its keys).
    pub(super) input_max_length: Option<i64>,
    pub(super) pattern: Option<(R::Pattern, Regex)>,
    /// `str` only: the form the text takes before any other check.
    pub(super) normalize: Normalize,
    pub(super) ge: Option<R::Limit>,
    pub(super) le: Option<R::Limit>,
    pub(super) gt: Option<R::Limit>,
    pub(super) lt: Option<R::Limit>,
    pub(super) one_of: Option<R::Choices>,
    /// The `Enum` class whose member replaces the value once it is checked.
    pub(super) member: Option<R::Member>,
    /// `Decimal` only: the digit counts the written value may have.
    pub(super) max_digits: Option<i64>,
    pub(super) decimal_places: Option<i64>,
    /// `int` and `Decimal` only: the step the value must be a multiple of.
    pub(super) multiple_of: Option<R::Limit>,
}

pub(super) struct Field<R: Rules> {
    pub(super) name: String,
    pub(super) wire_name: String,
    pub(super) node: Node<R>,
    pub(super) required: bool,
}

pub(super) struct Definition<R: Rules> {
    pub(super) class: R::Class,
    pub(super) fields: Vec<Field<R>>,
    pub(super) forbid_extra: bool,
}

/// A schema (and everything reachable from it) ready to validate.
#[pyclass(name = "CompiledSchema", module = "hinoki.core")]
pub struct Compiled {
    pub(super) definitions: Vec<Definition<PyRules>>,
    pub(super) root: usize,
    pub(super) uuid: Py<PyAny>,
    pub(super) from_iso: Py<PyAny>,
    pub(super) unset: Py<PyAny>,
    pub(super) native: Option<Arc<NativeSchema>>,
    /// The one-pass body decode of this schema, when it is inside its subset.
    pub(super) typed: Option<Arc<typeddecode::Typed>>,
    /// The nested-schema guides of the root's fields.
    pub(super) guides: FieldGuides,
    /// Cached JSON serialization plan for this class, built on first encode.
    pub(super) json_plan: PyOnceLock<Arc<JsonFieldPlan>>,
}

/// The GIL-free copy of a [`Compiled`] schema.
pub(crate) struct NativeSchema {
    pub(super) definitions: Vec<Definition<NativeRules>>,
    pub(super) root: usize,
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

#[derive(Clone, Copy, Default)]
pub(super) struct Normalize {
    strip: bool,
    lower: bool,
    upper: bool,
}

impl Normalize {
    pub(super) fn from_constraints(constraints: &Bound<'_, PyDict>) -> PyResult<Self> {
        Ok(Self {
            strip: super::compile::flag(constraints, "strip")?,
            lower: super::compile::flag(constraints, "lower")?,
            upper: super::compile::flag(constraints, "upper")?,
        })
    }

    pub(super) fn is_set(self) -> bool {
        self.strip || self.lower || self.upper
    }

    /// Python's `str.strip()` also removes the four information separators
    /// (U+001C to U+001F), which Unicode does not call white space.
    pub(super) fn apply(self, text: &str) -> Cow<'_, str> {
        let mut text = Cow::Borrowed(text);
        if self.strip {
            let trimmed =
                text.trim_matches(|c: char| c.is_whitespace() || ('\x1c'..='\x1f').contains(&c));
            if trimmed.len() != text.len() {
                text = Cow::Owned(trimmed.to_owned());
            }
        }
        if self.lower {
            text = Cow::Owned(text.to_lowercase());
        } else if self.upper {
            text = Cow::Owned(text.to_uppercase());
        }
        text
    }
}
